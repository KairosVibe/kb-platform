"""H04 装配与分桶的单元测试（**不需要数据库**）。

验证对象是 `app/services/authz_svc.py` 里两个**纯函数**：`assemble_unit_states`
与 `classify_units`。它们被特意抽成纯函数，就是为了让这几条安全规则可以离线验证
（真实 MySQL 行为另需集成测试，见 FUNCTION-MAP H04 的验证状态说明）。

重点覆盖三类最容易写错的地方：

1. **装配不得夹带字段**：`UnitState` 没有 `creator_id` 槽位，装配必须显式挑字段，
   否则"创建者旁路"会以数据泄漏的形式复活（PRD AC-04.08-01）。
2. **`unavailable` ≠ `denied`**：有权但索引没就绪，不能报成"无权限"。
3. **功能权限不产生正文读权**：即使上下文持有全部 14 个权限码，没有四维命中仍拒绝
   （H02 边界："功能权限不代替数据权限"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.core.permissions import PERMISSION_CODES
from app.engines.permission import UserCtx
from app.services.authz_svc import AclSubjects, assemble_unit_states, classify_units

USER_ID = 1
USER_DEPT = 100
USER_ROLE = 3


@dataclass
class FakeUnit:
    """假数据行（鸭子类型）。

    刻意**多带** `creator_id` / `title` / `code` 三个属性：装配函数若图省事用
    `**vars(row)` 之类的写法，这些字段就会泄进 `UnitState`，本文件的第 1 条用例
    会立刻失败。
    """

    id: int
    is_global: bool = False
    enabled: bool = True
    is_deleted: bool = False
    content_version: int = 1
    indexed_version: int | None = 1
    index_status: str = "indexed"
    acl_version: int = 1
    creator_id: int = 999
    title: str = "受限标题"
    code: str = "kb-001"


def _ctx(*, dept_id: int | None = USER_DEPT, roles: tuple[int, ...] = (USER_ROLE,),
         perms: tuple[str, ...] = ("ai:ask",)) -> UserCtx:
    return UserCtx(
        user_id=USER_ID,
        session_id=uuid4(),
        dept_id=dept_id,
        role_ids=frozenset(roles),
        permission_codes=frozenset(perms),
        identity_revision=1,
    )


def _acl(depts: tuple[int, ...] = (), roles: tuple[int, ...] = (), users: tuple[int, ...] = ()) -> AclSubjects:
    return AclSubjects(
        department_ids=frozenset(depts),
        role_ids=frozenset(roles),
        user_ids=frozenset(users),
    )


# ---------------------------------------------------------------- 装配

def test_assemble_drops_creator_and_other_row_fields() -> None:
    """装配只取契约字段，`creator_id` 等行字段不得进入判定输入。"""
    states = assemble_unit_states([FakeUnit(id=7)], {})
    state = states[0]
    for leaked in ("creator_id", "title", "code"):
        assert not hasattr(state, leaked), f"{leaked} 泄漏进 UnitState"


def test_assemble_maps_acl_and_empty_default() -> None:
    """ACL 三组 ID 正确映射；没有 ACL 的单元归一为**空集**而不是 None。"""
    states = assemble_unit_states(
        [FakeUnit(id=1), FakeUnit(id=2)],
        {1: _acl(depts=(100,), roles=(3,), users=(5,))},
    )
    by_id = {s.unit_id: s for s in states}
    assert by_id[1].department_ids == frozenset({100})
    assert by_id[1].role_ids == frozenset({3})
    assert by_id[1].user_ids == frozenset({5})
    assert by_id[2].department_ids == frozenset()
    assert by_id[2].role_ids == frozenset()


# ---------------------------------------------------------------- 四维放行

def test_global_grants_access_when_index_ready() -> None:
    result = classify_units(_ctx(), assemble_unit_states([FakeUnit(id=1, is_global=True)], {}))
    assert result.allowed == [1]
    assert result.denied == []
    assert result.versions == {1: 1}


def test_department_match_is_exact() -> None:
    """部门必须**精确匹配**：授权给 101 不因用户属于 100 而放行（无祖先/子孙继承）。"""
    hit = assemble_unit_states([FakeUnit(id=1)], {1: _acl(depts=(USER_DEPT,))})
    miss = assemble_unit_states([FakeUnit(id=2)], {2: _acl(depts=(USER_DEPT + 1,))})
    assert classify_units(_ctx(), hit).allowed == [1]
    assert classify_units(_ctx(), miss).denied == [2]


def test_role_intersection_grants_access() -> None:
    states = assemble_unit_states([FakeUnit(id=1)], {1: _acl(roles=(USER_ROLE,))})
    assert classify_units(_ctx(), states).allowed == [1]


def test_personal_grant_grants_access() -> None:
    states = assemble_unit_states([FakeUnit(id=1)], {1: _acl(users=(USER_ID,))})
    assert classify_units(_ctx(), states).allowed == [1]


def test_no_dimension_hit_is_denied() -> None:
    states = assemble_unit_states([FakeUnit(id=1)], {})
    result = classify_units(_ctx(), states)
    assert result.denied == [1]
    assert result.allowed == []


def test_permission_codes_do_not_grant_data_access() -> None:
    """★ 持有全部 14 个功能权限码，也不会因此读到未授权的正文。"""
    ctx = _ctx(perms=tuple(PERMISSION_CODES))
    states = assemble_unit_states([FakeUnit(id=1)], {})
    assert classify_units(ctx, states).denied == [1]


def test_deleted_and_disabled_denied_even_if_global() -> None:
    states = assemble_unit_states(
        [FakeUnit(id=1, is_global=True, is_deleted=True), FakeUnit(id=2, is_global=True, enabled=False)],
        {},
    )
    result = classify_units(_ctx(), states)
    assert result.denied == [1, 2]
    assert result.allowed == []
    assert result.unavailable == []


# ---------------------------------------------------------------- unavailable 与 denied 的区分

def test_permitted_but_not_indexed_goes_to_unavailable() -> None:
    """★ 有权但索引未就绪 → `unavailable`，**不是** `denied`。

    混进 `denied` 会对用户谎称"你没有权限"，也会让运营把建索引失败当成权限问题排查。
    """
    states = assemble_unit_states(
        [FakeUnit(id=1, is_global=True, index_status="pending", indexed_version=None)], {}
    )
    result = classify_units(_ctx(), states)
    assert result.unavailable == [1]
    assert result.denied == []
    assert result.allowed == []
    assert result.versions == {}


def test_stale_index_version_goes_to_unavailable() -> None:
    """内容已升版（2）但索引仍是旧版（1）→ 不得放行，也**不是**无权。"""
    states = assemble_unit_states(
        [FakeUnit(id=1, is_global=True, content_version=2, indexed_version=1)], {}
    )
    result = classify_units(_ctx(), states)
    assert result.unavailable == [1]
    assert result.denied == []
    assert result.versions == {}


def test_denied_takes_precedence_over_unavailable() -> None:
    """无权优先：既无权又索引未就绪的单元归 `denied`，不能靠 `unavailable` 掩盖拒绝。"""
    states = assemble_unit_states([FakeUnit(id=1, index_status="pending", indexed_version=None)], {})
    result = classify_units(_ctx(), states)
    assert result.denied == [1]
    assert result.unavailable == []


# ---------------------------------------------------------------- 输出形状

def test_versions_only_contains_allowed_units() -> None:
    states = assemble_unit_states(
        [
            FakeUnit(id=1, is_global=True),
            FakeUnit(id=2, is_global=True, index_status="stale", indexed_version=None),
        ],
        {},
    )
    result = classify_units(_ctx(), states)
    assert result.versions == {1: 1}
    assert result.unavailable == [2]


def test_empty_input_yields_empty_buckets() -> None:
    """空输入 → 三个桶全空。绝不能"没有输入等于没有限制"。"""
    result = classify_units(_ctx(), [])
    assert (result.allowed, result.denied, result.unavailable, result.versions) == ([], [], [], {})


def test_output_is_sorted_for_reviewability() -> None:
    """输出按 ID 升序：审计与用例比对需要稳定顺序（集合迭代顺序不可依赖）。"""
    states = assemble_unit_states([FakeUnit(id=9, is_global=True), FakeUnit(id=2, is_global=True)], {})
    result = classify_units(_ctx(), states)
    assert result.allowed == [2, 9]
    assert result.versions == {2: 1, 9: 1}


def test_no_department_user_is_denied_for_department_grant() -> None:
    """用户没有部门（`dept_id=None`）时，部门维度不可能命中。"""
    states = assemble_unit_states([FakeUnit(id=1)], {1: _acl(depts=(USER_DEPT,))})
    assert classify_units(_ctx(dept_id=None), states).denied == [1]
