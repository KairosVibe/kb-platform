"""种子数据定义的体检（**不需要数据库**）。

本机 MySQL 服务未启动，种子脚本无法执行；但"种子数据会不会引入安全旁路"这个问题
**必须在写库之前**就有答案，不能靠"跑一次看看"。因此把这几条固化成断言。

最重要的一条是 `test_seed_definitions_do_not_touch_acl`：它检查 `definitions.py`
里根本不出现 `knowledge_acl` / `acl` 字样。若有人为了"让管理员能看演示数据"
而在种子里塞 ACL 行，这条用例会失败并强迫其改为"演示文档在导入时显式配置授权"——
默认全空是安全契约的一部分（DESIGN_REVISION §2.1、PRD AC-04.08-01）。
"""

from __future__ import annotations

import inspect

from app.core.permissions import PERMISSION_CODES
from seeds import definitions
from seeds.definitions import (
    DEPARTMENTS,
    INITIAL_RETRIEVAL_CONFIG,
    ROLE_END_USER,
    ROLE_KB_ADMIN,
    ROLE_PERMISSIONS,
    ROLE_SYS_ADMIN,
    provider_snapshot,
)


def test_all_seeded_codes_are_registered() -> None:
    """每个种子权限码都必须在 `core.permissions` 注册过（权限码唯一来源）。"""
    seeded = {code for codes in ROLE_PERMISSIONS.values() for code in codes}
    assert seeded <= set(PERMISSION_CODES), sorted(seeded - set(PERMISSION_CODES))


def test_sys_admin_covers_all_functional_codes() -> None:
    """系统管理员覆盖全部 14 个**功能**权限码（这是功能完整性，不是数据读权）。"""
    assert set(ROLE_PERMISSIONS[ROLE_SYS_ADMIN]) == set(PERMISSION_CODES)
    assert len(ROLE_PERMISSIONS[ROLE_SYS_ADMIN]) == 14


def test_end_user_gets_only_ask() -> None:
    """普通提问者只有 `ai:ask`。

    ★ 刻意**不给 `kb:view`**：该码是"管理元数据"能力，PRD BC-04.01 明确它
      "不能扩展为正文读权"。给普通用户它会制造一种错误的印象——"看得见列表就能看正文"。
    """
    assert ROLE_PERMISSIONS[ROLE_END_USER] == ("ai:ask",)
    assert "kb:view" not in ROLE_PERMISSIONS[ROLE_END_USER]


def test_kb_admin_has_no_sys_codes() -> None:
    """知识管理员不应拿到系统管理类权限码（最小权限原则）。"""
    sys_codes = {c for c in ROLE_PERMISSIONS[ROLE_KB_ADMIN] if c.startswith("sys:")}
    assert not sys_codes, sorted(sys_codes)


def test_seed_definitions_do_not_touch_acl() -> None:
    """种子定义中不得出现任何 ACL 相关引用（防止默认可见性被种子数据打开）。"""
    source = inspect.getsource(definitions)
    # 去掉注释行后再查找，避免本用例的说明文字自己触发失败。
    code_lines = [
        line for line in source.splitlines() if not line.strip().startswith("#")
    ]
    body = "\n".join(code_lines)
    assert "knowledge_acl" not in body
    assert "is_global" not in body


def test_retrieval_defaults_match_function_map() -> None:
    """检索默认值与 FUNCTION-MAP §1 `config（Retrieval）` 一致。

    写成断言而不是直接引用配置对象，是因为这几个数是**契约值**：改动必须同时改文档。
    """
    assert INITIAL_RETRIEVAL_CONFIG == {
        "vector_top_k": 20,
        "keyword_top_k": 20,
        "max_per_route": 100,
        "answer_top_k": 5,
        "rrf_k": 60,
    }


def test_department_seeds_cover_prd_demo_scenarios() -> None:
    """PRD §1.4 的财务/人事正反例依赖"人力资源部"与"管理层"两个部门名。"""
    assert {"人力资源部", "管理层"} <= set(DEPARTMENTS)


def test_provider_snapshot_contains_no_secret_values() -> None:
    """provider 快照只允许放引用名（`secret_ref`），不允许放密钥值。

    DATA-CONTRACTS §2：`config_revision` 只保存允许的非密钥参数和 secret_ref。
    """
    snapshot = provider_snapshot(
        provider="qwen",
        model="text-embedding-v3",
        dimension=1024,
        model_version="qwen:text-embedding-v3:1024",
        endpoint="https://example.invalid/v1",
    )
    assert snapshot["secret_ref"].startswith("env:")
    # 不得出现"看起来像密钥"的字段名。
    assert not {k for k in snapshot if k in {"api_key", "token", "password", "secret"}}
    # 值里也不能夹带密钥（本用例以 sk- 前缀为主要形态做粗筛）。
    assert not any(isinstance(v, str) and v.startswith("sk-") for v in snapshot.values())


def test_dimension_frozen_to_1024() -> None:
    """D-03 已冻结 embedding 维度为 1024（2026-09-16 用户确认）。

    维度决定 Milvus collection schema 与索引，属不可逆结构：改它必须重建索引代际。
    """
    snapshot = provider_snapshot(
        provider="qwen",
        model="text-embedding-v3",
        dimension=1024,
        model_version="qwen:text-embedding-v3:1024",
        endpoint="https://example.invalid/v1",
    )
    assert snapshot["dimension"] == 1024
    assert snapshot["model_version"] == "qwen:text-embedding-v3:1024"
