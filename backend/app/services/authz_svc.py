"""H04 `authorize_units`：批量读取当前 ACL/状态/版本并分类。

分层依据 FUNCTION-MAP §0 第 3 条 与 H04 条目：**engines 只做纯算法，读库在本层**
（"纯 judge 只计算、authorize_units 负责读库"）。本模块只做三件事：

1. 批量读 `knowledge_unit` 与三张 ACL 表（**不使用懒加载**：逐条读会变成 N+1，
   而且在授权路径上"一次查询一台单元"会把延迟直接暴露给每一次提问）；
2. 把数据行装配成纯输入 `UnitState`（`assemble_unit_states`，纯函数，可离线测试）；
3. 调用纯判定 `filter_units` 分桶，再用 `is_retrievable` 把"放行但索引不可用"的单元
   单独挑出来。

★ 三个必须保持的区别（H04 边界条件原文："`unavailable`（索引不可用）必须与'无权'分开"）：

| 桶 | 含义 | 客户端可见性 |
|---|---|---|
| `allowed` | 有权且索引可用 | 可见，参与回答与引用 |
| `denied` | 无权（含已删除/停用/不存在） | 不可见；仅固定提示，明细只进受控审计 |
| `unavailable` | **有权但索引当前不可用** | 不是"无权"，不得混入 `denied` 的统计与提示 |

把 `unavailable` 混进 `denied` 会产生两种相反的危害：对用户谎称"你没有权限"，
对运营则把"索引没建好"记成"权限问题"，于是排查方向从一开始就是错的。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engines.permission import UserCtx, UnitState, filter_units, is_retrievable
from app.models import (
    KnowledgeAclDepartment,
    KnowledgeAclRole,
    KnowledgeAclUser,
    KnowledgeUnit,
)


@dataclass(frozen=True, slots=True)
class AclSubjects:
    """一个单元的四维授权对象 ID（取自三张强类型关联表）。"""

    department_ids: frozenset[int] = frozenset()
    role_ids: frozenset[int] = frozenset()
    user_ids: frozenset[int] = frozenset()


class AuthorizeUnitsResult(NamedTuple):
    """H04 输出。用 NamedTuple 是为了同时满足契约的"四元命名输出"与元组解包调用习惯。"""

    allowed: list[int]
    denied: list[int]
    unavailable: list[int]
    #: `unit_id -> content_version`：放行单元应当使用的**切片版本**（见 FUNCTION-MAP H04）。
    versions: dict[int, int]


def assemble_unit_states(
    units: Sequence[object], acl: Mapping[int, AclSubjects]
) -> list[UnitState]:
    """数据行 → `UnitState`（**纯函数**，不读库）。

    这里显式只取契约允许的字段，**不传 `creator_id`**：`UnitState` 没有该槽位，
    从类型上就不可能出现创建者旁路（PRD AC-04.08-01）。调整字段时必须同步
    FUNCTION-MAP §1 的 `UnitState` 定义。

    传入对象只需要具备相应属性（鸭子类型），因此测试可以用轻量假行对象覆盖
    "已删除/未索引/全局"等组合，而不需要起一个数据库。
    """
    states: list[UnitState] = []
    for row in units:
        unit_id = int(getattr(row, "id"))
        subjects = acl.get(unit_id) or AclSubjects()
        states.append(
            UnitState.from_raw(
                unit_id=unit_id,
                is_global=bool(getattr(row, "is_global")),
                department_ids=sorted(subjects.department_ids),
                role_ids=sorted(subjects.role_ids),
                user_ids=sorted(subjects.user_ids),
                enabled=bool(getattr(row, "enabled")),
                is_deleted=bool(getattr(row, "is_deleted")),
                content_version=int(getattr(row, "content_version")),
                indexed_version=getattr(row, "indexed_version"),
                index_status=str(getattr(row, "index_status")),
                acl_version=int(getattr(row, "acl_version")),
            )
        )
    return states


async def _load_acl(session: AsyncSession, unit_ids: Sequence[int]) -> dict[int, AclSubjects]:
    """一次读回三张 ACL 表（按 `unit_id` 批量过滤）。"""
    if not unit_ids:
        return {}
    depts: dict[int, set[int]] = defaultdict(set)
    roles: dict[int, set[int]] = defaultdict(set)
    users: dict[int, set[int]] = defaultdict(set)

    for model, bucket in (
        (KnowledgeAclDepartment, depts),
        (KnowledgeAclRole, roles),
        (KnowledgeAclUser, users),
    ):
        rows = await session.execute(
            select(model.unit_id, model.subject_id).where(model.unit_id.in_(list(unit_ids)))
        )
        for unit_id, subject_id in rows.all():
            bucket[int(unit_id)].add(int(subject_id))

    return {
        unit_id: AclSubjects(
            department_ids=frozenset(depts.get(unit_id, ())),
            role_ids=frozenset(roles.get(unit_id, ())),
            user_ids=frozenset(users.get(unit_id, ())),
        )
        for unit_id in set(depts) | set(roles) | set(users)
    }


def classify_units(ctx: UserCtx, states: Sequence[UnitState]) -> AuthorizeUnitsResult:
    """**纯分类**（不读库）：把已装配的单元切成 allowed / denied / unavailable。

    单独抽出来是因为这里的分类规则是安全相关的、而且**必须能离线测试**：
    `unavailable` 与 `denied` 的区分（H04 边界条件）只有在能构造各种组合时才验得住，
    若分类逻辑埋在一个需要数据库的函数里，最容易错的那部分反而最难测。

    顺序：先按 H03 判定读权，再对**放行者**判定索引可用性。反过来会让无权单元
    进入 `unavailable`，等于用一个更"温和"的桶掩盖了权限拒绝。
    """
    judged = filter_units(ctx, states)
    allowed_ids = set(judged.allowed_ids)

    allowed: list[int] = []
    unavailable: list[int] = []
    versions: dict[int, int] = {}
    for state in states:
        if state.unit_id not in allowed_ids:
            continue
        version = state.content_version
        if is_retrievable(state, chunk_version=version):
            allowed.append(state.unit_id)
            versions[state.unit_id] = version
        else:
            unavailable.append(state.unit_id)

    return AuthorizeUnitsResult(
        allowed=sorted(allowed),
        denied=sorted(set(judged.denied_ids)),
        unavailable=sorted(unavailable),
        versions=versions,
    )


async def authorize_units(
    session: AsyncSession,
    ctx: UserCtx,
    unit_ids: Sequence[int],
) -> AuthorizeUnitsResult:
    """H04：批量授权。

    契约签名 `authorize_units(ctx, unit_ids)` 中的 session 按 §0.5 由依赖注入提供
    （"DB session/文件存储/Provider 依赖由构造或请求作用域注入，不在每条业务签名重复列出"）。

    失败取向：**fail-closed**。查询不到的 ID 归入 `denied` 而不是忽略——忽略会让
    "查不到"等价于"没有限制"，这正是越权的典型成因。
    """
    ordered = list(dict.fromkeys(int(i) for i in unit_ids))
    if not ordered:
        # 空输入短路：不能返回"全部放行"，也不能返回错误。
        return AuthorizeUnitsResult(allowed=[], denied=[], unavailable=[], versions={})

    rows = (
        (await session.execute(select(KnowledgeUnit).where(KnowledgeUnit.id.in_(ordered))))
        .scalars()
        .all()
    )
    acl = await _load_acl(session, [int(row.id) for row in rows])
    states = assemble_unit_states(rows, acl)

    classified = classify_units(ctx, states)
    found = {state.unit_id for state in states}
    missing = [uid for uid in ordered if uid not in found]
    if missing:
        # 不存在的 ID 并入 denied（不额外暴露"是否存在"这一信息）。
        return classified._replace(denied=sorted(set(classified.denied) | set(missing)))
    return classified
