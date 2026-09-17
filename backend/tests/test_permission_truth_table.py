"""四维权限判定真值矩阵、索引可用性与受限输出契约测试（红线项）。

对应 FUNCTION-MAP.md §1（UserCtx / UnitState / FilterResult / RestrictedNotice）、
§4（H03 judge、H04 纯计算子步骤 filter_units 与 is_retrievable、H06 sanitize_denied、H36）、
DESIGN_REVISION.md §2.1 / §2.2、PRD.md §1 / §1.4、PRD.md AC-04.08-01、
ARCHITECTURE.md §3 / §4.1 / §4.2、PLAN.md §2。

设计说明：
- 负向用例与正向用例等量：权限模块的"误放行"比"误拒绝"严重得多；
- R3 删除的三条读权旁路（审计 PA-01/PA-02/PA-03）保留**拒绝预期**——
  旧例外用例已翻转为明确拒绝，未删除（PLAN §2 要求）；
- **数据读权（judge）与索引可用性（is_retrievable）必须分开**（ARCHITECTURE §4.2）：
  本文件专门断言"索引没准备好"不会被当成"无权"；
- 受限输出分"受控审计"与"客户端"两个出口（审计 PA-04/PA-05），对字段集合与文案做结构断言；
- 四维 16 种布尔组合的完整穷举矩阵仍待补齐（审计 PA-15）；
  H04 `authorize_units` 的读库部分待 M1-6（审计 PA-06 后半）。
"""

from __future__ import annotations

from uuid import UUID

import pytest

import app.core.permissions as permissions_module
from app.core.permissions import PERMISSIONS
from app.engines.permission import (
    ACCESS_RESTRICTED_NOTICE,
    PARTIAL_RESTRICTED_NOTICE,
    ClientReasonCode,
    DenyReason,
    FilterResult,
    RestrictedNotice,
    UnitState,
    UserCtx,
    filter_units,
    is_retrievable,
    judge,
    restricted_refusal,
    sanitize_denied,
)

# ---------------------------------------------------------------- 夹具
# 部门树：总公司(1) → 财务部(2) / 人力资源部(3) → 薪酬组(4) / 客服部(5) / 销售部(6)
# 角色：管理层(10) / 财务管理员(11) / 普通用户(12)
# ★ 部门树只用于构造历史数据关系；R3 判定不沿树泛化，故上下文只携带直属部门。
D_ROOT, D_FIN, D_HR, D_PAY, D_CS, D_SALES = 1, 2, 3, 4, 5, 6
R_MGMT, R_FINADMIN, R_USER = 10, 11, 12

U_SALES, U_HR, U_PAY, U_MGMT_ONLY, U_OTHER = 101, 102, 103, 104, 105

# 固定会话 ID：让测试可复现，不去比较随机 UUID
_SESSION = UUID("11111111-1111-1111-1111-111111111111")


def _ctx(
    user_id: int,
    *,
    dept_id: int | None = None,
    roles: tuple[int, ...] = (),
    codes: tuple[str, ...] = ("ai:ask",),
    identity_revision: int = 1,
) -> UserCtx:
    """构造服务端装配的 UserCtx（FUNCTION-MAP §1：六个字段缺一不可）。"""
    return UserCtx(
        user_id=user_id,
        session_id=_SESSION,
        dept_id=dept_id,
        role_ids=frozenset(roles),
        permission_codes=frozenset(codes),
        identity_revision=identity_revision,
    )


def ctx_sales() -> UserCtx:
    """销售部普通员工：直属部门=销售部，无角色。"""
    return _ctx(U_SALES, dept_id=D_SALES)


def ctx_hr() -> UserCtx:
    """人力资源部员工：直属部门=人力资源部。"""
    return _ctx(U_HR, dept_id=D_HR)


def ctx_pay() -> UserCtx:
    """薪酬组员工：直属部门=薪酬组（人力资源部的子部门）。"""
    return _ctx(U_PAY, dept_id=D_PAY)


def ctx_mgmt() -> UserCtx:
    """仅具管理层角色的用户（部门属财务部，与人力资源部无直接关系）。"""
    return _ctx(U_MGMT_ONLY, dept_id=D_FIN, roles=(R_MGMT,))


def unit_default(*, enabled: bool = True, is_deleted: bool = False) -> UnitState:
    """默认单元：未配任何权限（→ 默认拒绝）。"""
    return UnitState(unit_id=1, enabled=enabled, is_deleted=is_deleted)


# ---------------------------------------------------------------- 默认拒绝与无旁路


def test_t01_default_deny_for_everyone():
    """四维全空 → 任意人均拒绝（DESIGN_REVISION §2.1、PRD AC-04.08-01）。

    PA-02 翻转点：原用例断言"创建者例外放行"。R3 下判定输入类型不含 creator_id，
    "创建者"这一身份在授权链路中不存在。
    """
    assert judge(ctx_sales(), unit_default()) is DenyReason.DENY_DEFAULT
    assert judge(ctx_hr(), unit_default()) is DenyReason.DENY_DEFAULT
    assert judge(ctx_mgmt(), unit_default()) is DenyReason.DENY_DEFAULT


def test_unit_state_has_no_creator_field():
    """★ PA-02 结构性防线：判定输入类型不得含创建者字段，否则旁路随时可被重新引入。"""
    assert "creator_id" not in UnitState.__slots__
    assert not hasattr(UnitState(unit_id=1), "creator_id")

    # from_raw 也不接受该参数（传入会 TypeError，此处直接检查签名）
    import inspect

    params = inspect.signature(UnitState.from_raw).parameters
    assert "creator_id" not in params


def test_t10_no_super_admin_bypass():
    """不存在超管旁路：上下文无特权字段，管理员同样按四维规则逐项匹配。

    PA-01 翻转点：原用例断言 `UserCtx(is_super=True)` 放行。
    """
    # 结构性防线：字段本身不得重新出现
    assert "is_super" not in UserCtx.__slots__
    assert "dept_chain" not in UserCtx.__slots__

    # 具备管理角色的用户，在单元未授权时同样被拒
    assert judge(ctx_mgmt(), unit_default()) is DenyReason.DENY_DEFAULT


def test_permissions_module_has_no_super_admin_marker():
    """PA-01 第二处：`SUPER_ADMIN_ROLE_CODE` 旁路标记已删除，不得重新引入。"""
    assert not hasattr(permissions_module, "SUPER_ADMIN_ROLE_CODE")
    assert len(PERMISSIONS) == 14
    assert all("admin" not in p.code for p in PERMISSIONS)


def test_t19_authorization_ids_none_and_empty_equivalent():
    """三组授权 ID 为 None 与 [] 等价（API-CONTRACTS §1）。"""
    u_none = UnitState.from_raw(
        unit_id=1, is_global=False, department_ids=None, role_ids=None, user_ids=None,
    )
    u_empty = UnitState.from_raw(
        unit_id=1, is_global=False, department_ids=[], role_ids=[], user_ids=[],
    )
    assert u_none.department_ids == u_empty.department_ids == frozenset()
    assert judge(ctx_sales(), u_none) is DenyReason.DENY_DEFAULT
    assert judge(ctx_sales(), u_empty) is DenyReason.DENY_DEFAULT


# ---------------------------------------------------------------- 前置拦截：删除与停用


def test_deleted_unit_denied_even_if_global_and_for_everyone():
    """★ PA-06 新增：已删除单元先做墓碑拦截，`global=true` 也不能放行。

    依据：DATA-CONTRACTS §4"正文读取先执行墓碑检查"。
    """
    u = UnitState(unit_id=1, is_global=True, is_deleted=True)
    assert judge(ctx_sales(), u) is DenyReason.DENY_DELETED
    assert judge(ctx_hr(), u) is DenyReason.DENY_DELETED
    assert judge(ctx_mgmt(), u) is DenyReason.DENY_DELETED


def test_deleted_takes_precedence_over_disabled():
    """删除与停用同时成立时，原因码取墓碑（顺序即短路顺序，须稳定可断言）。"""
    u = UnitState(unit_id=1, enabled=False, is_deleted=True)
    assert judge(ctx_sales(), u) is DenyReason.DENY_DELETED


def test_disabled_unit_denied_for_everyone():
    """停用是"不可检索"的硬语义；管理员同样不能绕过。"""
    u = unit_default(enabled=False)
    assert judge(ctx_sales(), u) is DenyReason.DENY_DISABLED
    assert judge(ctx_hr(), u) is DenyReason.DENY_DISABLED
    assert judge(ctx_mgmt(), u) is DenyReason.DENY_DISABLED


# ---------------------------------------------------------------- 单通路


def test_t02_global_allows_everyone():
    assert judge(ctx_sales(), UnitState(unit_id=1, is_global=True)) is None
    assert judge(ctx_hr(), UnitState(unit_id=1, is_global=True)) is None


def test_t03_dept_exact_match():
    u = UnitState(unit_id=1, department_ids=frozenset({D_HR}))
    assert judge(ctx_hr(), u) is None


def test_t04_department_match_is_exact_only():
    """部门维度只比对**直属部门**，不沿部门树向上或向下泛化。

    DESIGN_REVISION §2.1；验收用例 A08「仅祖先部门匹配 → 拒绝」。

    PA-03 翻转点：原用例断言"权限配父部门、用户属子部门 → 命中"（祖先链泛化）。
    """
    perm_on_hr = UnitState(unit_id=1, department_ids=frozenset({D_HR}))
    assert judge(ctx_hr(), perm_on_hr) is None                              # 直属部门精确命中
    assert judge(ctx_pay(), perm_on_hr) is DenyReason.DENY_DEPT             # 子部门用户：不因父部门被授权而命中
    assert judge(ctx_sales(), perm_on_hr) is DenyReason.DENY_DEPT           # 无关部门

    perm_on_pay = UnitState(unit_id=1, department_ids=frozenset({D_PAY}))
    assert judge(ctx_hr(), perm_on_pay) is DenyReason.DENY_DEPT             # 父部门用户：不因子部门被授权而命中


def test_t06_role_match():
    u = UnitState(unit_id=1, role_ids=frozenset({R_MGMT}))
    assert judge(ctx_mgmt(), u) is None


def test_t07_multi_role_intersection():
    """任一角色命中即可（角色维度是集合交集，不是子集）。"""
    u = UnitState(unit_id=1, role_ids=frozenset({R_MGMT, R_FINADMIN}))
    ctx = _ctx(U_OTHER, roles=(R_FINADMIN, R_USER))
    assert judge(ctx, u) is None


def test_t08_user_dimension():
    u = UnitState(unit_id=1, user_ids=frozenset({U_SALES}))
    assert judge(ctx_sales(), u) is None
    assert judge(ctx_hr(), u) is DenyReason.DENY_USER


# ---------------------------------------------------------------- OR 组合穷举


@pytest.mark.parametrize(
    ("is_global", "department_ids", "role_ids", "user_ids", "expected"),
    [
        # 单通路命中（4 例）
        (True, set(), set(), set(), None),
        (False, {D_SALES}, set(), set(), None),
        (False, set(), {R_USER}, set(), None),
        (False, set(), set(), {U_SALES}, None),
        # 多通路同时命中（5 例）——OR 语义下仍为放行
        (True, {D_SALES}, {R_USER}, {U_SALES}, None),
        (False, {D_SALES}, {R_USER}, set(), None),
        (False, {D_SALES}, set(), {U_SALES}, None),
        (False, set(), {R_USER}, {U_SALES}, None),
        (False, {D_SALES}, {R_USER}, {U_SALES}, None),
        # 全部不命中（1 例）——配置了权限但与该用户无关
        (False, {D_HR}, {R_MGMT}, {U_OTHER}, DenyReason.DENY_DEPT),
    ],
)
def test_t11_or_semantics_exhaustive(is_global, department_ids, role_ids, user_ids, expected):
    """OR 四通路的组合穷举。任一通路满足即放行。

    注：完整 16 种布尔组合矩阵待补齐（审计 PA-15），此处保留既有 10 例。
    """
    u = UnitState(
        unit_id=1,
        is_global=is_global,
        department_ids=frozenset(department_ids),
        role_ids=frozenset(role_ids),
        user_ids=frozenset(user_ids),
    )
    ctx = _ctx(U_SALES, dept_id=D_SALES, roles=(R_USER,))
    assert judge(ctx, u) is expected


# ---------------------------------------------------------------- 边界与脏数据


def test_t14_deleted_reference_id_not_matching_and_no_error():
    """授权引用了已删除的部门/角色/用户 ID → 不命中且不抛异常。"""
    u = UnitState(
        unit_id=1,
        department_ids=frozenset({9998}), role_ids=frozenset({9999}),
        user_ids=frozenset({9997}),
    )
    result = judge(ctx_sales(), u)
    assert result is not None
    assert result in {DenyReason.DENY_DEPT, DenyReason.DENY_ROLE, DenyReason.DENY_USER}


def test_user_without_dept_can_still_match_role():
    """用户未分配部门（dept_id=None）时：部门维度不命中，但角色维度照常生效。"""
    ctx = _ctx(U_OTHER, dept_id=None, roles=(R_MGMT,))
    u = UnitState(unit_id=1, role_ids=frozenset({R_MGMT}))
    assert judge(ctx, u) is None


def test_user_without_dept_never_matches_dept_dimension():
    """dept_id=None 不得与任何部门授权"意外相等"（历史脏数据常见坑）。"""
    ctx = _ctx(U_OTHER, dept_id=None)
    assert judge(ctx, UnitState(unit_id=1, department_ids=frozenset({D_HR}))) is DenyReason.DENY_DEPT


def test_from_raw_tolerates_dirty_values():
    """脏数据（None 元素、字符串数字）不应导致查询失败。"""
    u = UnitState.from_raw(
        unit_id=1, is_global=False,
        department_ids=[None, "2", 2, "x"],   # type: ignore[list-item]
        role_ids=None, user_ids=[],
    )
    assert u.department_ids == frozenset({2})


# ---------------------------------------------------------------- 索引可用性（与读权正交）


def _indexed(**overrides) -> UnitState:
    """已完整索引的单元；用 overrides 制造"索引未就绪"的各种形态。"""
    base = {
        "unit_id": 1, "is_global": True, "enabled": True, "is_deleted": False,
        "content_version": 3, "indexed_version": 3, "index_status": "indexed",
    }
    base.update(overrides)
    return UnitState(**base)


def test_is_retrievable_true_when_versions_aligned():
    assert is_retrievable(_indexed(), chunk_version=3) is True


@pytest.mark.parametrize(
    ("overrides", "chunk_version"),
    [
        ({"index_status": "pending"}, 3),          # 尚未索引完成
        ({"index_status": "stale"}, 3),            # 替换/切片变更后的 stale 窗口
        ({"indexed_version": None}, 3),            # 从未成功索引
        ({"indexed_version": 2}, 3),               # 索引落后于内容版本
        ({}, 2),                                   # 候选切片来自旧版本
        ({"is_deleted": True}, 3),                 # 已删除
        ({"enabled": False}, 3),                   # 已停用
    ],
)
def test_is_retrievable_false_cases(overrides, chunk_version):
    """任一条不满足即不可检索：版本必须三方一致（ARCHITECTURE §4.1）。"""
    assert is_retrievable(_indexed(**overrides), chunk_version=chunk_version) is False


def test_index_not_ready_is_not_denied():
    """★ 核心分离断言（ARCHITECTURE §4.2）：索引未就绪**不等于**无权。

    若把该单元归入 denied，会导致"用户被误报为无权限"且错误地进入受限提示；
    正确行为是：有读权（judge 放行）但归入 unavailable。
    """
    u = _indexed(index_status="stale")
    assert judge(ctx_sales(), u) is None                    # 有读权
    assert is_retrievable(u, chunk_version=3) is False      # 但当前不可检索

    res = filter_units(ctx_sales(), [u])
    assert res.allowed_ids == [1]                           # 属于 allowed，不属于 denied
    assert res.denied_ids == []
    assert res.has_denied is False                          # 因此不追加受限提示


def test_deleted_and_index_status_are_independent_fields():
    """删除、停用、索引状态是三组独立字段（ARCHITECTURE §5），不得互相顶替。"""
    u = _indexed(is_deleted=True)
    assert judge(ctx_sales(), u) is DenyReason.DENY_DELETED   # 读权层已拦
    assert is_retrievable(u, chunk_version=3) is False        # 索引层也拦


# ---------------------------------------------------------------- 批量判定（仅 ID 出口）


def test_t13_filter_splits_allowed_and_denied_ids():
    """批量判定需精确切分，且**只返回单元 ID**。"""
    units = [
        UnitState(unit_id=1, is_global=True),
        UnitState(unit_id=2, department_ids=frozenset({D_HR})),
        UnitState(unit_id=3, department_ids=frozenset({D_SALES})),
    ]
    res = filter_units(ctx_sales(), units)
    assert res.allowed_ids == [1, 3]
    assert res.denied_ids == [2]
    assert res.denied_count == 1
    assert res.has_denied is True


def test_filter_result_exposes_ids_only():
    """★ 安全断言：批量判定结果结构只有两个 ID 列表。

    PA-04 关闭点：原 `FilterResult.denied` 携带 `DeniedUnit(title, missing_hint)`，
    会向调用方泄漏受限单元的标题与 ACL 实体名单（DESIGN_REVISION §2.2 禁止）。
    """
    assert FilterResult.__slots__ == ("allowed_ids", "denied_ids")
    res = filter_units(ctx_sales(), [UnitState(unit_id=9, department_ids=frozenset({D_HR}))])
    assert res.denied_ids == [9]
    for forbidden in ("title", "content", "text", "snippet", "acl", "dept_names", "missing_hint"):
        assert forbidden not in FilterResult.__slots__
        assert not hasattr(res, forbidden)


def test_filter_units_empty_input():
    """空候选集不得被当成"拒绝"（避免把"没检索到"误判为"无权"）。"""
    res = filter_units(ctx_sales(), [])
    assert res.allowed_ids == [] and res.denied_ids == []
    assert res.has_denied is False


# ---------------------------------------------------------------- 受限输出契约（H06/H36）


def test_internal_deny_reasons_are_not_client_visible():
    """★ 两个出口的取值域必须不重叠：内部明细原因码不得进入客户端枚举。"""
    client_values = {c.value for c in ClientReasonCode}
    assert client_values == {"ACCESS_RESTRICTED"}
    assert not (client_values & {r.value for r in DenyReason})


def test_sanitize_denied_returns_none_when_nothing_denied():
    """未发生拒绝时不追加任何提示（全部有权路径不得被污染）。"""
    assert sanitize_denied(False) is None


def test_sanitize_denied_returns_fixed_partial_notice():
    """部分无权：固定追加提示，文案取自 DESIGN_REVISION §2.2 原文。"""
    notice = sanitize_denied(True)
    assert notice is not None
    assert notice.reason_code is ClientReasonCode.ACCESS_RESTRICTED
    assert notice.message == PARTIAL_RESTRICTED_NOTICE == "部分参考资料因权限受限无法展示"


def test_restricted_refusal_returns_fixed_notice():
    """全部无权：固定拒绝提示，结构同为 RestrictedNotice。"""
    notice = restricted_refusal()
    assert notice.reason_code is ClientReasonCode.ACCESS_RESTRICTED
    assert notice.message == ACCESS_RESTRICTED_NOTICE


def test_restricted_notice_carries_no_metadata():
    """★ PA-04 核心断言：客户端提示只含固定原因码与固定文案，无任何受限元数据。"""
    notices = [sanitize_denied(True), restricted_refusal()]
    for notice in notices:
        assert notice is not None
        assert RestrictedNotice.__slots__ == ("reason_code", "message")
        # 不含数字 → 不泄漏单元 ID、数量或版本号
        assert not any(ch.isdigit() for ch in notice.message)
        # 不含正文内容字段
        assert not hasattr(notice, "content")
        assert not hasattr(notice, "unit_id")


def test_partial_and_full_notices_share_reason_code_but_differ_in_wording():
    """两条固定提示共用同一 reason_code，但文案不同（区分"部分"与"全部"）。"""
    partial = sanitize_denied(True)
    full = restricted_refusal()
    assert partial is not None
    assert partial.reason_code is full.reason_code
    assert partial.message != full.message


def test_notices_are_stable_across_calls():
    """固定提示必须是常量：重复调用返回等价结构，避免"顺带"注入可变信息。"""
    assert sanitize_denied(True) == sanitize_denied(True)
    assert restricted_refusal() == restricted_refusal()


# ---------------------------------------------------------------- 场景复现


def test_scenario_finance_salary_isolation():
    """复现财务薪酬隔离场景：公开资料 vs 仅 HR/管理层可读。

    《差旅报销标准》全员公开 → 所有用户可读；
    《高管薪酬与股权激励细则》仅人力资源部**直属**与**管理层角色**可读。

    PA-03 翻转点：原用例断言"薪酬组（子部门）因祖先授权而放行"，
    R3 下薪酬组用户属子部门，不继承人力资源部的授权 → 拒绝。
    """
    travel = UnitState(unit_id=1, is_global=True)
    salary = UnitState(
        unit_id=2, department_ids=frozenset({D_HR}), role_ids=frozenset({R_MGMT})
    )

    assert judge(ctx_sales(), travel) is None
    assert judge(ctx_hr(), travel) is None

    assert judge(ctx_sales(), salary) is DenyReason.DENY_DEPT
    assert judge(ctx_hr(), salary) is None          # 直属部门精确命中
    assert judge(ctx_pay(), salary) is DenyReason.DENY_DEPT   # 子部门不继承祖先授权
    assert judge(ctx_mgmt(), salary) is None        # 角色通路


def test_scenario_partial_restricted_keeps_authorized_evidence():
    """混合场景：部分无权时保留有权内容，并只追加固定提示。

    DESIGN_REVISION §2.2「部分无权」；PRD §1「普通问答受限提示只有固定message」。
    """
    units = [
        UnitState(unit_id=1, is_global=True, index_status="indexed", indexed_version=1,
                  content_version=1),
        UnitState(unit_id=2, department_ids=frozenset({D_HR})),
        UnitState(unit_id=3, department_ids=frozenset({D_HR}), role_ids=frozenset({R_MGMT})),
    ]
    res = filter_units(ctx_sales(), units)
    assert res.allowed_ids == [1]              # 仅公开资料进入检索
    assert res.denied_ids == [2, 3]            # 仅用于受控审计

    notice = sanitize_denied(res.has_denied)
    assert notice is not None
    assert notice.message == PARTIAL_RESTRICTED_NOTICE
    # 提示中不得出现被拒单元的数量（"2 份"之类）
    assert "2" not in notice.message


def test_scenario_all_restricted_produces_refusal_without_generation():
    """全部无权：产出固定拒绝提示，无任何有权证据可供生成。

    DESIGN_REVISION §2.2「全部无权」；caller 据此走 result_type=access_restricted，
    不调用生成 Provider（该约束由服务层落地，此处断言其输入条件成立）。
    """
    units = [
        UnitState(unit_id=1, department_ids=frozenset({D_HR})),
        UnitState(unit_id=2, department_ids=frozenset({D_PAY})),
    ]
    res = filter_units(ctx_sales(), units)
    assert res.allowed_ids == []
    assert res.has_denied is True

    notice = restricted_refusal()
    assert notice.message == ACCESS_RESTRICTED_NOTICE
    assert not any(ch.isdigit() for ch in notice.message)


# ---------------------------------------------------------------- 四维 16 组合穷举（PA-15）

# 四维布尔组合全集，顺序为 (global, 直属部门, 角色, 个人)。
# ★ 显式列出而非用 itertools.product：**矩阵本身是验收对象**（PRD §1.4「四维16种布尔组合」），
#   应当肉眼可数——写成推导式会让"少写一行"这类错误藏起来。
FOUR_DIM_MATRIX: tuple[tuple[bool, bool, bool, bool], ...] = (
    (False, False, False, False),
    (False, False, False, True),
    (False, False, True, False),
    (False, False, True, True),
    (False, True, False, False),
    (False, True, False, True),
    (False, True, True, False),
    (False, True, True, True),
    (True, False, False, False),
    (True, False, False, True),
    (True, False, True, False),
    (True, False, True, True),
    (True, True, False, False),
    (True, True, False, True),
    (True, True, True, False),
    (True, True, True, True),
)


def _unit_for(combo: tuple[bool, bool, bool, bool]) -> UnitState:
    """把一组布尔组合翻译成单元配置：命中→配置为与该用户匹配；未命中→不配置该维度。"""
    is_global, dept_hit, role_hit, user_hit = combo
    return UnitState(
        unit_id=1,
        is_global=is_global,
        department_ids=frozenset({D_SALES}) if dept_hit else frozenset(),
        role_ids=frozenset({R_USER}) if role_hit else frozenset(),
        user_ids=frozenset({U_SALES}) if user_hit else frozenset(),
    )


def test_four_dimension_matrix_is_complete_and_unique():
    """矩阵必须恰好 16 行且互不重复——防止"补测"时少写或复制粘贴重复。"""
    assert len(FOUR_DIM_MATRIX) == 16
    assert len(set(FOUR_DIM_MATRIX)) == 16
    assert set(FOUR_DIM_MATRIX) == set(
        (g, d, r, u)
        for g in (False, True)
        for d in (False, True)
        for r in (False, True)
        for u in (False, True)
    )


@pytest.mark.parametrize(("is_global", "dept_hit", "role_hit", "user_hit"), FOUR_DIM_MATRIX)
def test_four_dimension_truth_table_16_combinations(is_global, dept_hit, role_hit, user_hit):
    """★ PA-15：四维 16 种布尔组合穷举。

    FUNCTION-MAP §4 H03「验收矩阵」；PRD §1.4 单元验收。
    断言：**至少一维命中即放行；四维全不命中即拒绝**，且全不命中时原因码为 `DENY_DEFAULT`
    （四维全空 = 默认拒绝，见 DESIGN_REVISION §2.1）。
    """
    ctx = _ctx(U_SALES, dept_id=D_SALES, roles=(R_USER,))
    unit = _unit_for((is_global, dept_hit, role_hit, user_hit))

    should_allow = is_global or dept_hit or role_hit or user_hit
    expected = None if should_allow else DenyReason.DENY_DEFAULT
    assert judge(ctx, unit) is expected


def test_four_dimension_matrix_allows_exactly_fifteen_of_sixteen():
    """独立复核矩阵语义：16 种组合中恰好 15 种放行、1 种拒绝（OR 的必然结果）。

    这条与上面的参数化用例互为校验——参数化可能被整体改错，计数不会。
    """
    ctx = _ctx(U_SALES, dept_id=D_SALES, roles=(R_USER,))
    verdicts = [judge(ctx, _unit_for(combo)) is None for combo in FOUR_DIM_MATRIX]
    assert verdicts.count(True) == 15
    assert verdicts.count(False) == 1
    assert verdicts[-1] is True                       # 全 True 组合必须放行
    assert verdicts[0] is False                       # 全 False 组合必须拒绝


@pytest.mark.parametrize(
    ("department_ids", "role_ids", "user_ids", "expected"),
    [
        # 只配部门且不匹配 → 部门
        (frozenset({D_HR}), frozenset(), frozenset(), DenyReason.DENY_DEPT),
        # 部门优先于角色与个人
        (frozenset({D_HR}), frozenset({R_MGMT}), frozenset(), DenyReason.DENY_DEPT),
        (frozenset({D_HR}), frozenset(), frozenset({U_HR}), DenyReason.DENY_DEPT),
        # 部门未配置 → 角色
        (frozenset(), frozenset({R_MGMT}), frozenset(), DenyReason.DENY_ROLE),
        # 角色优先于个人
        (frozenset(), frozenset({R_MGMT}), frozenset({U_HR}), DenyReason.DENY_ROLE),
        # 只剩个人
        (frozenset(), frozenset(), frozenset({U_HR}), DenyReason.DENY_USER),
        # 四维全空 → 默认拒绝
        (frozenset(), frozenset(), frozenset(), DenyReason.DENY_DEFAULT),
    ],
)
def test_deny_reason_priority_is_stable(department_ids, role_ids, user_ids, expected):
    """内部原因码按"部门 → 角色 → 个人 → 默认"选取（FUNCTION-MAP §4 H03 逻辑第 3 条）。

    该顺序只影响受控审计的可复现性，不影响放行结果；但顺序不稳定会让审计结论无法比对。
    """
    ctx = _ctx(U_SALES, dept_id=D_SALES, roles=(R_USER,))
    unit = UnitState(
        unit_id=1, department_ids=department_ids, role_ids=role_ids, user_ids=user_ids
    )
    assert judge(ctx, unit) is expected
