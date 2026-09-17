"""模型配置与运行控制（M09，FUNCTION-MAP §3 F-09.01—F-09.04）。

`config_revision` 是**可热改业务参数**的持久载体（append-only）：
- `patch` = 本次变更的键值；`snapshot` = 合并后的全量快照；
- 问答/挖掘请求绑定创建时的 revision（"同一次问答用哪套阈值"可事后复算）；
- **embedding 相关键冻结**（PRD BC-09.02）：provider/model/dim 变化 = 换向量空间，
  必须换代际重建，接口直接 409 拒绝——热改 embedding 是"静默混查"的入口；
- **密钥不回读**：`key_configured` 只回答"设没设"，值来自运行时注入。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import TaskError
from app.core.response import BizError
from app.db.repository import UnitOfWork
from app.models import ConfigRevision
from app.providers import embedding as embedding_provider
from app.services.ingest_svc import _log

#: 可热改白名单：键 → (类型, 整数取值范围)。白名单外的键一律 422（能力边界校验）。
_WHITELIST: dict[str, tuple[type, tuple[int, int] | None]] = {
    "models.chat_model": (str, None),
    "models.rerank_model": (str, None),
    "thresholds.vector_top_k": (int, (5, 100)),
    "thresholds.keyword_top_k": (int, (5, 100)),
    "thresholds.answer_top_k": (int, (1, 20)),
    "thresholds.rrf_k": (int, (10, 200)),
    "thresholds.semantic_faq": (int, (50, 99)),   # 语义匹配门槛，百分比
    "thresholds.cluster": (int, (50, 99)),        # 聚类门槛，百分比
}
#: embedding 相关键：变化 = 换向量空间，热改直接拒绝。
_EMBEDDING_KEYS = ("models.embed_provider", "models.embed_model", "models.embed_dim")


def _defaults_from_settings(settings: Any) -> dict[str, Any]:
    return {
        "models": {
            "chat_model": settings.llm_model,
            "rerank_model": settings.rerank_model,
            "embed_provider": settings.embed_provider,
            "embed_model": settings.embed_model,
            "embed_dim": settings.embed_dim,
        },
        "thresholds": {
            "vector_top_k": 20, "keyword_top_k": 20, "answer_top_k": 5, "rrf_k": 60,
            "semantic_faq": 92, "cluster": 90,
        },
        "limits": {},
    }


async def _latest_revision(session: AsyncSession) -> ConfigRevision | None:
    return (
        await session.execute(
            select(ConfigRevision).order_by(ConfigRevision.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------- F-09.01


async def read_config(session: AsyncSession, ctx: Any) -> dict[str, Any]:
    """F-09.01：读当前配置快照。密钥只显示是否设置，不从接口读回。"""
    latest = await _latest_revision(session)
    settings = get_settings()
    snapshot = dict(latest.snapshot) if latest is not None else _defaults_from_settings(settings)
    revision = int(latest.id) if latest is not None else 0
    return {
        "revision": revision,
        "models": dict(snapshot.get("models") or {}),
        "thresholds": dict(snapshot.get("thresholds") or {}),
        "limits": dict(snapshot.get("limits") or {}),
        "key_configured": bool(settings.dashscope_api_key.get_secret_value()),
    }


# ---------------------------------------------------------------- F-09.02


def _validate_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """白名单 + 类型 + 范围校验。embedding 键直接 409。"""
    flat: dict[str, Any] = {}
    for section, values in (patch or {}).items():
        if not isinstance(values, dict):
            raise BizError("INVALID_ARGUMENT", f"{section} 必须为对象")
        for key, value in values.items():
            flat[f"{section}.{key}"] = value
    for key in flat:
        if key in _EMBEDDING_KEYS:
            raise BizError(
                "REINDEX_REQUIRED",
                "embedding 配置变更等于更换向量空间，必须走换代际重建流程，禁止热改",
            )
        if key not in _WHITELIST:
            raise BizError("INVALID_ARGUMENT", f"不支持热改的配置项：{key}")
        value_type, value_range = _WHITELIST[key]
        if value_type is int:
            if isinstance(value := flat[key], bool) or not isinstance(value := flat[key], int):
                raise BizError("INVALID_ARGUMENT", f"{key} 必须为整数")
            if value_range and not value_range[0] <= value <= value_range[1]:
                raise BizError(
                    "INVALID_ARGUMENT", f"{key} 必须在 {value_range[0]}—{value_range[1]}"
                )
            flat[key] = int(flat[key])
        else:
            if not isinstance(flat[key], value_type) or not str(flat[key]).strip():
                raise BizError("INVALID_ARGUMENT", f"{key} 必须为非空字符串")
    return flat


async def update_config(
    session: AsyncSession, ctx: Any, *, patch: dict[str, Any], expected_revision: int
) -> dict[str, Any]:
    """F-09.02：保存配置（sys:model）。白名单校验 → 新 revision（append-only）。"""
    latest = await _latest_revision(session)
    current_revision = int(latest.id) if latest is not None else 0
    if current_revision != int(expected_revision):
        raise BizError("REVISION_CONFLICT")
    flat_patch = _validate_patch(patch)

    base = dict(latest.snapshot) if latest is not None else _defaults_from_settings(ctx)
    snapshot: dict[str, Any] = {}
    for section in ("models", "thresholds", "limits"):
        merged = dict(base.get(section) or {})
        for key, value in flat_patch.items():
            if key.startswith(f"{section}."):
                merged[key.split(".", 1)[1]] = value
        snapshot[section] = merged

    new_revision = ConfigRevision(patch=flat_patch, snapshot=snapshot, actor_id=int(ctx.user_id))
    session.add(new_revision)
    await session.flush()
    _log(session, ctx.user_id, "config.update", "config_revision", int(new_revision.id),
         dict(latest.patch) if latest is not None else None, flat_patch)
    return {"revision": int(new_revision.id)}


# ---------------------------------------------------------------- F-09.03


async def probe_provider(session: AsyncSession, ctx: Any, *, provider: str) -> dict[str, Any]:
    """F-09.03：连通探测（sys:model）。只对允许的提供方做最小调用；有调用成本。"""
    if provider not in ("embedding", "generation"):
        raise BizError("INVALID_ARGUMENT", "provider 必须为 embedding/generation")
    settings = get_settings()
    started = time.monotonic()
    try:
        if provider == "embedding":
            await embedding_provider.embed_batches(["probe"], settings)
        else:
            from app.core.cancel import CancelSignal
            from app.providers import generation as generation_provider

            async for _delta in generation_provider.stream_llm(
                [{"role": "user", "content": "ping"}], settings, CancelSignal(lambda: False)
            ):
                break  # 最小探测：拿到首段即认为连通
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000),
                "error_code": None}
    except TaskError as exc:
        return {"ok": False, "latency_ms": int((time.monotonic() - started) * 1000),
                "error_code": exc.code}
    except Exception:  # noqa: BLE001 — 安全错误分类，不暴露原始异常
        return {"ok": False, "latency_ms": int((time.monotonic() - started) * 1000),
                "error_code": "DEPENDENCY_UNAVAILABLE"}


# ---------------------------------------------------------------- F-09.04


async def readiness(session: AsyncSession, now: Any) -> dict[str, Any]:
    """F-09.04：就绪检查（GET /ready；内部探针，不含 secret）。

    首版检查项：db（连通）、config（存在快照）、vector（Milvus 可达）。
    "executor" 在当前架构 = 受理时进程内 spawn 后台任务，天然就绪。
    外部瞬断使 ready=False，但不导致无限重启（探针无副作用）。
    """
    checks: dict[str, bool] = {}
    try:
        await session.execute(select(ConfigRevision.id).limit(1))
        checks["db"] = True
        checks["config"] = (await _latest_revision(session)) is not None
    except Exception:  # noqa: BLE001
        checks["db"] = False
        checks["config"] = False
    try:
        from app.providers import vector_store

        client = vector_store._connect(get_settings())
        try:
            client.has_collection(get_settings().milvus_collection)
        finally:
            client.close()
        checks["vector"] = True
    except Exception:  # noqa: BLE001 — 瞬断只降级 ready，不重试
        checks["vector"] = False
    checks["executor"] = True  # 受理即 spawn（见 chat_svc.spawn_answer）
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "version": "0.1.0",
    }
