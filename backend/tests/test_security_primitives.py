"""安全原语与部署配置的契约测试（审计步骤 4：PA-07 / PA-09 / PA-10）。

对应 FUNCTION-MAP.md §3 F-01.01（密码长度契约）/ F-01.02（一次性消费与持久化）/
F-09.02（embedding 变更必须重建）、§4 H01、§1 `config（Provider）`、
ARCHITECTURE.md §3（第 6 条）、DATA-CONTRACTS.md §2、PRD.md AC-01.01-02 / BC-09.02。

覆盖的三个历史缺陷：

- PA-09：`verify_password` 原先把 >72 字节**静默截断**，导致"前 72 字节相同"的
  两个不同密码互相通过——本文件用该攻击的具体形式做回归断言；
- PA-07：撤销表原先是进程内 dict，重启即遗忘，且**不会阻止误上生产**；
- PA-10：配置把本地模型描述为"配额耗尽时的应急降级"，与"禁止混查不同向量空间"冲突。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import bcrypt
import pytest

from app.core.config import Settings, get_settings
from app.core.security import (
    PASSWORD_MAX_BYTES,
    PASSWORD_MIN_BYTES,
    InMemoryRefreshRevocationStore,
    PasswordPolicyError,
    get_refresh_store,
    hash_password,
    verify_password,
)

_SESSION = UUID("22222222-2222-2222-2222-222222222222")


def _settings(**overrides: object) -> Settings:
    """不读 .env 的 Settings（测试需确定性，避免本机 .env 干扰）。"""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------- PA-09 密码长度契约


def test_hash_password_accepts_boundary_lengths():
    """12 与 72 字节是闭区间端点，必须都能创建。"""
    assert hash_password("a" * PASSWORD_MIN_BYTES).startswith("$2")
    assert hash_password("b" * PASSWORD_MAX_BYTES).startswith("$2")


def test_hash_password_rejects_empty_and_too_short():
    with pytest.raises(PasswordPolicyError):
        hash_password("")
    with pytest.raises(PasswordPolicyError):
        hash_password("a" * (PASSWORD_MIN_BYTES - 1))


def test_hash_password_rejects_too_long():
    with pytest.raises(PasswordPolicyError):
        hash_password("a" * (PASSWORD_MAX_BYTES + 1))


def test_hash_password_is_salted():
    """同一密码两次哈希结果不同（未加盐会让彩虹表直接命中）。"""
    assert hash_password("a" * 16) != hash_password("a" * 16)


def test_verify_password_roundtrip():
    hashed = hash_password("correct horse battery")
    assert verify_password("correct horse battery", hashed) is True
    assert verify_password("wrong horse battery", hashed) is False


def test_verify_password_rejects_over_72_instead_of_truncating():
    """★ PA-09 回归断言：超长密码必须显式拒绝，**不得截断后比对**。

    截断实现下，`base + "X"` 的前 72 字节与 `base` 相同 → 会错误地校验通过。
    """
    base = "a" * PASSWORD_MAX_BYTES
    hashed = hash_password(base)

    with pytest.raises(PasswordPolicyError):
        verify_password(base + "X", hashed)
    with pytest.raises(PasswordPolicyError):
        verify_password("z" * (PASSWORD_MAX_BYTES + 100), hashed)

    # 未截断的正确路径仍然可用
    assert verify_password(base, hashed) is True


def test_verify_password_allows_legacy_short_password():
    """登录必须兼容既有短密码（F-01.01：最小长度只在创建/改密时校验）。"""
    legacy = bcrypt.hashpw(b"short", bcrypt.gensalt(rounds=4)).decode("utf-8")
    assert verify_password("short", legacy) is True
    assert verify_password("shortt", legacy) is False


def test_verify_password_returns_false_on_corrupted_hash():
    """哈希串损坏按校验失败处理，不抛异常暴露内部状态。"""
    assert verify_password("whatever", "not-a-bcrypt-hash") is False
    assert verify_password("whatever", "") is False
    assert verify_password("", "$2b$12$abcdefghijklmnopqrstuv") is False


# ---------------------------------------------------------------- PA-07 撤销表


def test_in_memory_store_refuses_non_dev_env():
    """★ PA-07 核心断言：进程内撤销表不得被带上生产——非 dev 构造即失败。"""
    store = InMemoryRefreshRevocationStore(app_env="dev")
    assert len(store) == 0

    for env in ("prod", "staging", "test", ""):
        with pytest.raises(RuntimeError, match="仅限 dev"):
            InMemoryRefreshRevocationStore(app_env=env)


def test_consume_is_one_time_only():
    """一次性消费：同一 jti 只有首次成功（AC-01.02-01 旧令牌不可重放）。"""
    store = InMemoryRefreshRevocationStore(app_env="dev")
    args = {"auth_session_id": _SESSION, "expires_at": 10_000, "now": 1_000}

    assert store.consume("jti-1", **args) is True
    assert store.consume("jti-1", **args) is False      # 重放
    assert store.is_consumed("jti-1") is True
    # 另一个 jti（同会话的后续轮换）可以正常消费
    assert store.consume("jti-2", **args) is True


def test_consume_rejected_after_session_revoked():
    """会话撤销后，该会话下任何刷新令牌都不可再消费（F-01.03 退出即失效）。"""
    store = InMemoryRefreshRevocationStore(app_env="dev")
    assert store.revoke_session(_SESSION, expires_at=10_000) is True
    assert store.is_session_revoked(_SESSION) is True
    assert store.consume("jti-x", auth_session_id=_SESSION, expires_at=10_000) is False


def test_revoke_session_is_idempotent():
    """重复注销不报错、不再是"新撤销"（F-01.03 重复退出幂等）。"""
    store = InMemoryRefreshRevocationStore(app_env="dev")
    assert store.revoke_session(_SESSION, expires_at=10_000) is True
    assert store.revoke_session(_SESSION, expires_at=10_000) is False
    # 其他会话不受影响
    other = uuid4()
    assert store.is_session_revoked(other) is False


def test_purge_expired_removes_only_expired_entries():
    store = InMemoryRefreshRevocationStore(app_env="dev")
    session = uuid4()
    store.consume("old", auth_session_id=session, expires_at=500, now=100)
    store.consume("fresh", auth_session_id=session, expires_at=5_000, now=100)
    store.revoke_session(uuid4(), expires_at=400)

    assert len(store) == 3
    assert store.purge_expired(now=1_000) == 2          # 1 个过期 token + 1 个过期会话
    assert store.is_consumed("old") is False
    assert store.is_consumed("fresh") is True


def test_get_refresh_store_follows_app_env(monkeypatch):
    """工厂按部署环境决策：dev 可用，prod 直接失败（而不是静默降级）。"""
    get_settings.cache_clear()
    get_refresh_store.cache_clear()
    try:
        assert isinstance(get_refresh_store(), InMemoryRefreshRevocationStore)

        monkeypatch.setenv("APP_ENV", "prod")
        get_settings.cache_clear()
        get_refresh_store.cache_clear()
        with pytest.raises(RuntimeError, match="仅限 dev"):
            get_refresh_store()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        get_refresh_store.cache_clear()


# ---------------------------------------------------------------- PA-10 向量空间标识


def test_embedding_model_version_encodes_all_three_components():
    """版本串必须同时编码 provider / model / dimension——三者任一变化即换向量空间。"""
    base = _settings()
    assert base.embedding_model_version == f"qwen:text-embedding-v3:{base.embed_dim}"

    assert _settings(embed_model="text-embedding-v4").embedding_model_version != base.embedding_model_version
    assert _settings(embed_dim=768).embedding_model_version != base.embedding_model_version
    assert _settings(embed_provider="local_bge").embedding_model_version != base.embedding_model_version


def test_local_provider_is_a_deployment_choice_not_a_fallback_path():
    """PA-10：`local_bge` 是另一套部署配置，切换必然改变向量空间标识。

    配置层不提供任何"保留旧 provider 但仍复用旧向量"的表达能力——
    即不存在热降级开关（PRD BC-09.02）。
    """
    qwen = _settings(embed_provider="qwen")
    local = _settings(embed_provider="local_bge")
    assert qwen.embedding_model_version.startswith("qwen:")
    assert local.embedding_model_version.startswith("local_bge:")
    assert local.embedding_model_version != qwen.embedding_model_version
