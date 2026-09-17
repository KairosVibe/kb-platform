"""四维混合数据权限判定引擎与受限输出契约。

对应 FUNCTION-MAP.md §1 / §2.1 / §4（H03、H04 纯计算子步骤、H05、H06、H36）、
DESIGN_REVISION.md §2、ARCHITECTURE.md §3/§4.1、PRD.md §1、PLAN.md §2 已决事项。

★ 这是全项目最关键的安全模块，判定部分设计为**纯函数**：
  - 无 IO、无数据库访问、无全局状态；
  - 输入输出完全由参数决定 → 可对真值矩阵穷举单测（PRD §1.4 单元验收）。
  字段名与 FUNCTION-MAP §1 逐字段一致，唯一例外是 `global`→`is_global`（Python 关键字）。

## 判定语义（充分条件 OR，短路求值）

    放行 ⟺ 全局公开 ∨ 直属部门命中 ∨ 角色交集命中 ∨ 个人命中

前置拦截（任一命中即拒绝，不再进入四维判定）：

    已删除（墓碑）→ 停用

安全基调：
  - **默认拒绝**：权限全空且非全局 → 任何人均不可读；
  - **没有旁路**：创建者与系统管理员**不自动获得正文读权**（DESIGN_REVISION §2.1、
    PRD AC-04.08-01）；判定输入类型 `UnitState` **不含 creator_id**，从类型上排除该旁路；
  - **部门精确匹配**：只比对直属部门，不继承祖先或子孙（验收用例 A08）；
  - **fail-closed**：异常路径按拒绝处理。

## 两个正交判定（不可合并）

| 判定 | 函数 | 回答的问题 | 失败归入 |
|---|---|---|---|
| 数据读权 | `judge` / `filter_units` | 这个用户**能不能看**这个单元 | `denied_ids` |
| 索引可用性 | `is_retrievable` | 这个单元的**当前版本是否已索引可查** | `unavailable_ids` |

ARCHITECTURE §4.2：无权、无证据、低置信、索引不可用、服务失败必须分开——
**"索引没准备好"不等于"无权"**，也不进知识缺口。

## 受限输出契约（PA-04/PA-05，2026-09-16 已关闭）

判定结果分**两个出口**，结构上物理分离，禁止混用：

| 出口 | 结构 | 允许携带的信息 |
|---|---|---|
| 受控审计 | `FilterResult.denied_ids`、`DenyReason` 明细 | 单元 ID 与内部拒绝原因码；用于 `qa_audit.denied_snapshot` 与运营诊断 |
| 客户端 | `RestrictedNotice` | **只有** `reason_code=ACCESS_RESTRICTED` 与固定 message |

DESIGN_REVISION §2.2：客户端 denied **不含受限 ID、标题、摘要、部门名单、正文或数量**；
完整受限列表仅保留在受控审计中。管理侧需要查看某单元的授权配置时，走带 `kb:perm`
校验的 `GET /api/knowledge-units/{id}/acl`（API-CONTRACTS §5 API-S04）。

## R3 移除的三条旁路（审计编号见 docs/PERMISSION-CONTRACT-AUDIT.md）

  - PA-01 `UserCtx.is_super` 超管读权旁路 —— 字段已删除，不得重新引入；
  - PA-02 创建者自动读权 —— `UnitState` 不再携带 `creator_id`；
  - PA-03 祖先部门链命中 —— `dept_chain` 已删除，改为直属部门精确匹配。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class DenyReason(StrEnum):
    """内部拒绝原因码。

    ★ 仅用于内部判定分类与**受控审计**（`qa_audit.denied_snapshot` 等）。
    **严禁下发给客户端**——客户端只允许看到 `ClientReasonCode.ACCESS_RESTRICTED`。
    """

    DENY_DEFAULT = "DENY_DEFAULT"      # 单元未配置任何权限（默认拒绝）
    DENY_DEPT = "DENY_DEPT"            # 部门维度不匹配
    DENY_ROLE = "DENY_ROLE"            # 角色维度不匹配
    DENY_USER = "DENY_USER"            # 个人维度不匹配
    DENY_DISABLED = "DENY_DISABLED"    # 单元已停用
    DENY_DELETED = "DENY_DELETED"      # 单元已逻辑删除（墓碑）


class ClientReasonCode(StrEnum):
    """**客户端可见**的受限原因码。

    DESIGN_REVISION §2.2 规定客户端 denied 只含本枚举与固定 message，
    不含受限 ID、标题、摘要、部门名单、正文或数量。
    故本枚举有意只保留一个成员——新增成员前必须先修订该契约。
    """

    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"


# 部分无权时追加的固定提示。文案取自 DESIGN_REVISION §2.2 原文，不得改写。
PARTIAL_RESTRICTED_NOTICE = "部分参考资料因权限受限无法展示"

# 全部无权时的固定拒绝提示。契约只要求"固定受限提示"，未固定具体文案；
# 本句为项目选择：只说明超出权限范围，不暗示受限资料是否存在（避免成为存在性探测口）。
ACCESS_RESTRICTED_NOTICE = "该问题超出当前权限范围，无法作答"

# 索引就绪状态取值（ARCHITECTURE §5：pending/indexed/stale）。
# 有意使用字面量而非新增枚举类型：本模块的公开符号须逐项登记在 FUNCTION-MAP，
# 不为单点比较引入未登记的公共类型。
_INDEX_STATUS_INDEXED = "indexed"


@dataclass(frozen=True, slots=True)
class RestrictedNotice:
    """下发客户端的受限提示。

    ★ 结构由 `__slots__` 锁死：只允许 reason_code 与 message 两个字段。
    任何"顺手带上 unit_id / 标题 / 数量"的改动都会破坏契约（DESIGN_REVISION §2.2），
    并由单元测试直接断言字段集合。
    """

    reason_code: ClientReasonCode
    message: str


@dataclass(frozen=True, slots=True)
class UserCtx:
    """用户权限上下文（FUNCTION-MAP §1 UserCtx）。

    由 H01 `auth_core.authenticate` 服务端装配后传入，客户端不得构造。
    - `permission_codes` 是**功能权限**，由 H02 `require_permission` 检查；
      它不产生任何知识正文读权——正文读权只由 `judge` 判定。
    - `identity_revision` 用于识别"权限已变更"，供撤权/失效判断使用。
    - ★ 不设 `is_super`（系统管理员与普通用户同权）、不设祖先部门链（部门精确匹配）。
    """

    user_id: int
    session_id: UUID
    dept_id: int | None
    role_ids: frozenset[int]
    permission_codes: frozenset[str]
    identity_revision: int


@dataclass(frozen=True, slots=True)
class UnitState:
    """知识单元的授权与索引状态（FUNCTION-MAP §1 UnitState）。

    ★ **不含 `creator_id`**：创建者信息只存在于数据行与审计，不进入授权输入——
    没有该字段，就不可能出现"创建者旁路"（PRD AC-04.08-01、DATA-CONTRACTS §2）。

    `department_ids`/`role_ids`/`user_ids` 是**单元被授权给谁**（四维配置，默认全空）；
    与 `UserCtx.role_ids`（用户拥有哪些角色）语义不同，判定时取交集。
    """

    unit_id: int
    is_global: bool = False
    department_ids: frozenset[int] = field(default_factory=frozenset)
    role_ids: frozenset[int] = field(default_factory=frozenset)
    user_ids: frozenset[int] = field(default_factory=frozenset)
    enabled: bool = True
    is_deleted: bool = False
    content_version: int = 1
    indexed_version: int | None = None
    index_status: str = "pending"
    acl_version: int = 1

    @classmethod
    def from_raw(
        cls,
        *,
        unit_id: int,
        is_global: bool,
        department_ids: Sequence[int] | None,
        role_ids: Sequence[int] | None,
        user_ids: Sequence[int] | None,
        enabled: bool = True,
        is_deleted: bool = False,
        content_version: int = 1,
        indexed_version: int | None = None,
        index_status: str = "pending",
        acl_version: int = 1,
    ) -> UnitState:
        """从数据库原始值构造。

        统一在这里归一化三组授权 ID，避免判定逻辑里到处写 `if x or []`。
        非法元素（None、非 int）被忽略而非抛异常——脏数据不应导致查询失败。

        ★ 有意不接收 `creator_id`：见类文档串。
        """
        def _norm(values: Sequence[int] | None) -> frozenset[int]:
            if not values:
                return frozenset()
            out: set[int] = set()
            for v in values:
                try:
                    out.add(int(v))
                except (TypeError, ValueError):
                    continue
            return frozenset(out)

        return cls(
            unit_id=unit_id,
            is_global=bool(is_global),
            department_ids=_norm(department_ids),
            role_ids=_norm(role_ids),
            user_ids=_norm(user_ids),
            enabled=bool(enabled),
            is_deleted=bool(is_deleted),
            content_version=content_version,
            indexed_version=indexed_version,
            index_status=index_status,
            acl_version=acl_version,
        )


@dataclass(frozen=True, slots=True)
class FilterResult:
    """批量判定的内部结果（权限切分，不含索引可用性）。

    ★ 两个出口的边界在此体现：本结构**只返回单元 ID**，不携带标题、ACL 名单或数量。
      - `allowed_ids`：有权候选（是否可检索还需过 `is_retrievable`）；
      - `denied_ids`：无权候选，**仅供受控审计**（`qa_audit.denied_snapshot`），
        禁止下发给客户端。
    """

    allowed_ids: list[int] = field(default_factory=list)
    denied_ids: list[int] = field(default_factory=list)

    @property
    def allowed_count(self) -> int:
        return len(self.allowed_ids)

    @property
    def denied_count(self) -> int:
        return len(self.denied_ids)

    @property
    def has_denied(self) -> bool:
        """是否存在被拒候选——决定是否追加部分受限提示，或归为 access_restricted。"""
        return bool(self.denied_ids)


def judge(ctx: UserCtx, unit: UnitState) -> DenyReason | None:
    """判定单个用户对单个单元的**数据读权**。

    Returns:
        None 表示放行；否则返回**内部**拒绝原因码（不得下发客户端）。

    判定顺序即短路顺序，越靠前越"廉价且强"：
        已删除 → 停用 → 全局 → 直属部门 → 角色 → 个人 → 拒绝

    ★ R3 语义：创建者、系统管理员均不在此函数内获得任何特权——判定只依据
    单元的四维配置与用户的直属部门/角色/个人标识。
    ★ 索引是否就绪**不在本函数**判定，见 `is_retrievable`（两者必须分开）。
    """
    # 1) 已删除单元先做墓碑拦截（DATA-CONTRACTS §4：正文读取先执行墓碑检查）
    if unit.is_deleted:
        return DenyReason.DENY_DELETED

    # 2) 停用的单元对任何人都不可检索（含管理员），保证"停用"语义彻底
    if not unit.enabled:
        return DenyReason.DENY_DISABLED

    # 3) 全局公开
    if unit.is_global:
        return None

    # 4) 部门：**仅直属部门精确匹配**。
    #    用户属子部门、权限配给父部门 → 不命中；用户属父部门、权限配给子部门 → 同样不命中。
    #    任何"沿部门树上下泛化"的行为都是隐式扩权（验收用例 A08）。
    if ctx.dept_id is not None and ctx.dept_id in unit.department_ids:
        return None

    # 5) 角色：集合有交集即可（支持多角色）
    if ctx.role_ids and (ctx.role_ids & unit.role_ids):
        return None

    # 6) 个人
    if ctx.user_id in unit.user_ids:
        return None

    # 7) 全部落空。原因码取"最贴近"的维度，供内部判定分类与受控审计使用。
    #    创建者在此与普通用户完全一致：类型里根本没有 creator_id。
    if unit.department_ids:
        return DenyReason.DENY_DEPT
    if unit.role_ids:
        return DenyReason.DENY_ROLE
    if unit.user_ids:
        return DenyReason.DENY_USER
    return DenyReason.DENY_DEFAULT


def is_retrievable(unit: UnitState, *, chunk_version: int) -> bool:
    """判定单元的**当前索引版本**是否可用于检索（H04 索引用性子步骤）。

    放行条件（ARCHITECTURE §4.1）：`enabled`、未删除、`index_status=indexed`、
    且 `content_version = indexed_version = chunk_version`。

    与 `judge` 正交：本函数不回答"能不能看"，只回答"现在查得到吗"。
    不满足的单元归入 `unavailable_ids`，与 `denied_ids` **分开**处理——
    "索引还没准备好"不得被当成"无权"，也不进知识缺口。

    Args:
        unit: 单元状态（含内容版本与已索引版本）。
        chunk_version: 候选切片所属的内容版本（由向量/切片元数据带出）。

    Returns:
        True 表示该版本的切片可以进入检索结果。
    """
    if unit.is_deleted or not unit.enabled:
        return False
    if unit.index_status != _INDEX_STATUS_INDEXED:
        return False
    if unit.indexed_version is None:
        return False
    # 三个版本必须一致：内容版本 = 已索引版本 = 候选切片版本。
    # 索引未追平内容（如替换文档期间的 stale 窗口）或候选来自旧版本，一律不可返回，
    # 避免"替换期间召回旧内容"（ARCHITECTURE §4.1）。
    return unit.indexed_version == unit.content_version == chunk_version


def filter_units(ctx: UserCtx, units: Sequence[UnitState]) -> FilterResult:
    """批量判定候选单元的**数据读权**，切分"有权"与"无权"两路（只返回 ID）。

    Args:
        ctx: 用户上下文。
        units: 待判定单元的状态序列。

    Returns:
        FilterResult。**调用方必须只把 allowed_ids 对应的正文送进 Prompt**；
        denied_ids 只能写入受控审计，任何情况下不得下发客户端。
        是否可检索需再对 allowed_ids 逐个调用 `is_retrievable`。

    注意（fail-closed 的外延）：本函数本身不吞异常。若上层在异常处理中调用本函数，
    必须把异常路径视为"全部拒绝"而非"全部放行"——这是安全红线的实现要求。
    """
    allowed: list[int] = []
    denied: list[int] = []
    for unit in units:
        if judge(ctx, unit) is None:
            allowed.append(unit.unit_id)
        else:
            denied.append(unit.unit_id)
    return FilterResult(allowed_ids=allowed, denied_ids=denied)


# ---------------------------------------------------------------- H04（读库侧）

async def authorize_units(
    session: Any, ctx: UserCtx, unit_ids: Sequence[int]
) -> tuple[list[int], list[int], list[int], dict[int, int]]:
    """H04：批量读库装配 `UnitState` 并逐项判定，输出三桶 + 版本表。

    ★ 本函数是授权引擎里**唯一**的 IO 步骤：`judge`/`filter_units` 保持纯函数
      （可穷举单测），本函数只做"读库 → 组装 → 调用纯函数 → 分桶"。
      三桶互斥：`denied`（无读权，只进受控审计）、`unavailable`（**有权**但索引
      不可用——"索引没准备好"不等于"无权"，ARCHITECTURE §4.2）、`allowed`。

    Returns:
        (allowed, denied, unavailable, versions)；`versions[unit_id] = content_version`
        供调用方做版本一致性复核（H04 契约第 4 个返回值）。
    """
    from sqlalchemy import select

    from app.models import (
        KnowledgeAclDepartment,
        KnowledgeAclRole,
        KnowledgeAclUser,
        KnowledgeUnit,
    )

    ids = sorted({int(v) for v in unit_ids})
    if not ids:
        return [], [], [], {}

    unit_rows = (
        (await session.execute(select(KnowledgeUnit).where(KnowledgeUnit.id.in_(ids))))
        .scalars()
        .all()
    )
    dept_map, role_map, user_map = await _load_acl_maps(session, ids)

    states: list[UnitState] = []
    for row in unit_rows:
        unit_id = int(row.id)
        states.append(
            UnitState.from_raw(
                unit_id=unit_id,
                is_global=row.is_global,
                department_ids=dept_map.get(unit_id, ()),
                role_ids=role_map.get(unit_id, ()),
                user_ids=user_map.get(unit_id, ()),
                enabled=row.enabled,
                is_deleted=row.is_deleted,
                content_version=int(row.content_version),
                indexed_version=row.indexed_version,
                index_status=row.index_status,
                acl_version=int(row.acl_version),
            )
        )

    result = filter_units(ctx, states)
    allowed: list[int] = []
    unavailable: list[int] = []
    versions: dict[int, int] = {}
    for unit in states:
        versions[unit.unit_id] = unit.content_version
        if unit.unit_id not in result.allowed_ids:
            continue
        # 有读权再看索引用性：以"当前内容版本"为候选版本复核。
        if is_retrievable(unit, chunk_version=unit.content_version):
            allowed.append(unit.unit_id)
        else:
            unavailable.append(unit.unit_id)
    return allowed, result.denied_ids, unavailable, versions


async def _load_acl_maps(
    session: Any, unit_ids: list[int]
) -> tuple[dict[int, set[int]], dict[int, set[int]], dict[int, set[int]]]:
    """三张 ACL 表各一次 `IN` 查询（避免每单元三次往返的 N+1）。"""
    from sqlalchemy import select

    from app.models import KnowledgeAclDepartment, KnowledgeAclRole, KnowledgeAclUser

    dept: dict[int, set[int]] = {}
    role: dict[int, set[int]] = {}
    user: dict[int, set[int]] = {}
    for model, target in (
        (KnowledgeAclDepartment, dept),
        (KnowledgeAclRole, role),
        (KnowledgeAclUser, user),
    ):
        rows = (
            await session.execute(
                select(model.unit_id, model.subject_id).where(model.unit_id.in_(unit_ids))
            )
        ).all()
        for unit_id, subject_id in rows:
            target.setdefault(int(unit_id), set()).add(int(subject_id))
    return dept, role, user


def sanitize_denied(has_denied: bool) -> RestrictedNotice | None:
    """H06：部分无权时给出**固定**追加提示，未发生拒绝时返回 None。

    DESIGN_REVISION §2.2「部分无权」：仅使用有权证据，并追加
    "部分参考资料因权限受限无法展示"。

    ★ 本函数是无副作用的纯函数，正是为了让它无法"顺带"拿到被拒单元的信息——
    入参只有一个布尔量，从签名上杜绝受限元数据泄漏。
    """
    if not has_denied:
        return None
    return RestrictedNotice(
        reason_code=ClientReasonCode.ACCESS_RESTRICTED,
        message=PARTIAL_RESTRICTED_NOTICE,
    )


def restricted_refusal() -> RestrictedNotice:
    """H36：全部无权时的固定拒绝提示（`result_type=access_restricted`）。

    DESIGN_REVISION §2.2「全部无权」：固定受限提示，**不调用生成模型猜测答案**。
    ARCHITECTURE §3 同款：该终态由服务层直接产出，不调用生成 Provider。
    """
    return RestrictedNotice(
        reason_code=ClientReasonCode.ACCESS_RESTRICTED,
        message=ACCESS_RESTRICTED_NOTICE,
    )
