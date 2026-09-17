"""向量化提供方（H11，FUNCTION-MAP §4）。

`embed_batches(texts, config)` 按 `config.embed_batch_size`（DashScope 单请求上限 10）
分批调用 **OpenAI 兼容模式 REST**（`config.dashscope_base_url`），用 httpx 直调而不引入
dashscope SDK：SDK 的版本矩阵是额外未知数，而兼容端点的请求/响应形状是公开稳定的。

测试替身（用户决策"B 先行、A 补验"）：`embed_batches` 是模块级函数，测试用
monkeypatch 替换本函数即可注入确定性向量——不需要在配置里加"fake"枚举值
（那会污染部署期配置的契约）。真实链路由 E2E 脚本用真实 key 补验。

三条契约要点在此落地：
1. **数量/维度/有限值校验**：返回向量与输入一一对应、维度 == `embed_dim`、无 NaN/Inf。
2. **超长不静默截断**：单条超限直接报永久错误。*"重新切片"*的职责在 H21——嵌入层
   1 文本 = 1 向量，在这里切片会破坏"输入↔向量"的对齐，切片窗口由 chunk_config 控制。
3. **限流最多 3 次退避**：429/5xx 指数退避重试，3 次仍失败 = 瞬时失败（交给 retry_wait）。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Any

import httpx

from app.core.errors import TaskError
from app.tasks.parsing import estimate_tokens

#: DashScope text-embedding-v3 单条输入的 token 上限（官方文档值）。
_SINGLE_TEXT_TOKEN_LIMIT = 8000

#: 限流重试参数（契约"限流最多3次退避"）。
_MAX_RETRIES = 3
_RETRY_BACKOFF = (0.5, 1.0, 2.0)


async def embed_batches(
    texts: list[str], config: Any
) -> tuple[list[list[float]], dict[str, int], str]:
    """H11：批量向量化。返回 `(vectors, usage, model_version)`，与输入**一一对应**。"""
    if not texts:
        return [], {"prompt_tokens": 0, "total_tokens": 0}, _model_version(config)

    for index, text in enumerate(texts):
        if estimate_tokens(text) > _SINGLE_TEXT_TOKEN_LIMIT:
            raise TaskError(
                "CHUNK_TOO_LONG",
                f"第 {index} 条切片超出单条 token 上限（>{_SINGLE_TEXT_TOKEN_LIMIT}）；"
                "应调小 chunk_config.size 重新切片，而不是在此层截断",
                transient=False,
            )

    size = max(1, int(config.embed_batch_size))
    vectors: list[list[float]] = []
    usage = {"prompt_tokens": 0, "total_tokens": 0}
    for start in range(0, len(texts), size):
        batch = texts[start: start + size]
        batch_vectors, batch_usage = await _embed_one_batch(batch, config)
        vectors.extend(batch_vectors)
        usage["prompt_tokens"] += batch_usage.get("prompt_tokens", 0)
        usage["total_tokens"] += batch_usage.get("total_tokens", 0)
    return vectors, usage, _model_version(config)


def _model_version(config: Any) -> str:
    """向量空间标识（与 Settings.embedding_model_version 同口径）。"""
    return f"{config.embed_provider}:{config.embed_model}:{config.embed_dim}"


async def _embed_one_batch(
    batch: list[str], config: Any
) -> tuple[list[list[float]], dict[str, int]]:
    """单批调用。限流/服务端错误退避重试；4xx 其他错误为永久失败。"""
    api_key = config.dashscope_api_key.get_secret_value()
    if not api_key:
        raise TaskError(
            "EMBED_NO_CREDENTIAL", "未配置 DASHSCOPE_API_KEY，无法调用向量化服务", transient=False
        )
    url = f"{config.dashscope_base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": config.embed_model,
        "input": batch,
        "dimensions": config.embed_dim,
        "encoding_format": "float",
    }

    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            last_error = f"网络错误：{exc}"
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF[attempt])
                continue
            raise TaskError("EMBED_UNREACHABLE", last_error, transient=True) from exc

        if response.status_code == 200:
            return _validate_response(response.json(), batch, config)
        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF[attempt])
                continue
            raise TaskError("EMBED_RATE_LIMITED", f"重试 {_MAX_RETRIES} 次后仍失败：{last_error}", transient=True)
        # 其余 4xx：请求本身有问题（模型名/参数/凭据），重试无意义。
        raise TaskError(
            "EMBED_REJECTED",
            f"HTTP {response.status_code}：{response.text[:200]}",
            transient=False,
        )
    raise TaskError("EMBED_RATE_LIMITED", f"重试后仍失败：{last_error}", transient=True)


def _validate_response(
    data: dict[str, Any], batch: list[str], config: Any
) -> tuple[list[list[float]], dict[str, int]]:
    """契约校验：按返回 index 对齐、数量一致、维度一致、全部为有限值。"""
    items = data.get("data") or []
    if len(items) != len(batch):
        raise TaskError(
            "EMBED_MISMATCH",
            f"返回向量数 {len(items)} != 输入条数 {len(batch)}",
            transient=True,
        )
    ordered: list[list[float]] = [None] * len(batch)  # type: ignore[assignment]
    for item in items:
        index = int(item.get("index", 0))
        vector = item.get("embedding") or []
        if len(vector) != int(config.embed_dim):
            raise TaskError(
                "EMBED_DIM_MISMATCH",
                f"返回维度 {len(vector)} != 配置维度 {config.embed_dim}——"
                "等于更换了向量空间，必须新建代际（PRD BC-09.02）",
                transient=False,
            )
        if not all(math.isfinite(value) for value in vector):
            raise TaskError("EMBED_NON_FINITE", "返回向量含 NaN/Inf", transient=True)
        ordered[index] = [float(value) for value in vector]
    if any(v is None for v in ordered):
        raise TaskError("EMBED_MISMATCH", "返回 index 有缺口", transient=True)

    usage_raw = data.get("usage") or {}
    return ordered, {
        "prompt_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "total_tokens": int(usage_raw.get("total_tokens", 0)),
    }
