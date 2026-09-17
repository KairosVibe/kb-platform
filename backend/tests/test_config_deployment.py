"""部署配置的形态约束测试（依据 DEPLOYMENT §9.2）。

★ 本文件只验证"配置能否正确加载"，**不连任何中间件**——连通性证据在 DEPLOYMENT §9.1
  的端到端往返，两者不要互相冒充。

全部用例用 `Settings(_env_file=None, ...)`，避免本机 `.env` 干扰结果
（与 tests/test_security_primitives.py 同一手法）。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    """不读 .env 的 Settings：测试需确定性。"""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 向量库双形态


def test_default_form_is_standalone_with_http_uri() -> None:
    """默认形态必须是"在本机可用"的那个。

    `milvus_lite` 仅支持 Linux/macOS，而本机开发平台是 Windows——把 lite 设为默认
    等于发一个在本机必然不可用的配置。D-08 已把中间件放在 VM 上，故默认取 standalone。
    """
    settings = _settings()
    assert settings.vector_backend == "milvus_standalone"
    assert settings.milvus_uri.startswith("http://")


def test_standalone_with_file_path_fails_fast() -> None:
    """standalone 配文件路径必须**启动期**失败。

    若不在启动期拦，它会一路连到错误形态，直到第一次检索才以"连不上/空结果"暴露，
    归因成本远高于一条启动错误。
    """
    with pytest.raises(ValueError, match="milvus_standalone"):
        _settings(vector_backend="milvus_standalone", milvus_uri="./data/milvus_lite.db")


def test_lite_with_http_uri_fails_fast() -> None:
    with pytest.raises(ValueError, match="milvus_lite"):
        _settings(vector_backend="milvus_lite", milvus_uri="http://127.0.0.1:19530")


def test_lite_with_file_path_is_accepted() -> None:
    """lite 形态（Linux/云端）仍须可用——校验不能写成"只准 standalone"。"""
    settings = _settings(vector_backend="milvus_lite", milvus_uri="./data/milvus_lite.db")
    assert settings.vector_backend == "milvus_lite"


def test_https_uri_is_accepted_for_standalone() -> None:
    """托管 Milvus 走 https，不能被误判为非法。"""
    settings = _settings(
        vector_backend="milvus_standalone", milvus_uri="https://milvus.example.com:19530"
    )
    assert settings.milvus_uri.startswith("https://")


# ---------------------------------------------------------------- Neo4j


def test_neo4j_uri_must_use_bolt_or_neo4j_scheme() -> None:
    """http:// 这类地址在 bolt 驱动下不会启动期报错，而是连接期超时——故前移为校验。"""
    with pytest.raises(ValueError, match="NEO4J_URI"):
        _settings(neo4j_uri="http://127.0.0.1:7474")


def test_neo4j_uri_accepts_both_valid_schemes() -> None:
    assert _settings(neo4j_uri="bolt://127.0.0.1:7687").neo4j_uri.startswith("bolt://")
    assert _settings(neo4j_uri="neo4j://127.0.0.1:7687").neo4j_uri.startswith("neo4j://")


def test_neo4j_password_is_secret() -> None:
    """口令必须是 SecretStr：否则 repr 与日志会把它带出来。"""
    settings = _settings(neo4j_password="s3cret-probe-value")
    assert isinstance(settings.neo4j_password, SecretStr)
    assert "s3cret-probe-value" not in repr(settings)


def test_neo4j_password_defaults_to_empty() -> None:
    """空口令是允许的加载状态（图召回尚未落地，不构成启动前置）。

    但**不允许**用空口令去连库——那属运行时行为，由调用方在连接前显式检查；
    这里只锁定"默认值是空，而不是某个看似可用的占位口令"。
    """
    assert _settings().neo4j_password.get_secret_value() == ""
