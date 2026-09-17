"""生成提供方（H13 `providers.stream_llm`）。

真实链路：DashScope **OpenAI 兼容模式** `/chat/completions`，`stream=true` +
`stream_options.include_usage`（拿真实用量——"未知用量不填 0"），httpx 直调不引 SDK。

测试替身（同 H11 的策略）：`stream_llm` 是模块级函数，测试 monkeypatch 替换。
真实 E2E 用真实 key 补验。

三条契约要点：
1. **首段输出后不透明重试**——流已经开始就只能往前走，出错交给终态 failed；
2. **协作取消**：每个 delta 之间检查 `cancel`，取消即停止（已生成部分留痕）；
3. **未知用量 null**：拿不到 usage 就 `ModelDelta(usage=None)`，绝不填 0。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from app.core.cancel import CancelSignal
from app.core.errors import TaskError


@dataclass(frozen=True, slots=True)
class ModelDelta:
    """一段模型输出。`text` 可为空串（仅携带 usage/finish_reason 的收尾帧）。"""

    text: str
    usage: dict[str, int | None] | None = None
    finish_reason: str | None = None


async def stream_llm(
    messages: list[dict[str, str]], config: Any, cancel: CancelSignal
) -> AsyncIterator[ModelDelta]:
    """H13：流式生成。逐 delta 产出；取消/错误语义见模块注释。"""
    api_key = config.dashscope_api_key.get_secret_value()
    if not api_key:
        raise TaskError("GENERATION_NO_CREDENTIAL", "未配置 DASHSCOPE_API_KEY", transient=False)

    url = f"{config.dashscope_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": config.llm_model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    try:
        async with httpx.AsyncClient(timeout=float(config.llm_timeout_s)) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    # 未产出任何文本前的失败属于瞬时/可重试分类交给终态；4xx 参数类为永久。
                    transient = response.status_code == 429 or response.status_code >= 500
                    raise TaskError(
                        "GENERATION_REJECTED",
                        f"HTTP {response.status_code}：{body[:200]}",
                        transient=transient,
                    )
                async for line in response.aiter_lines():
                    if cancel.cancelled:
                        return
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    chunk = json.loads(data)
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    usage_raw = chunk.get("usage")
                    usage = None
                    if usage_raw:
                        usage = {
                            "prompt_tokens": usage_raw.get("prompt_tokens"),
                            "completion_tokens": usage_raw.get("completion_tokens"),
                        }
                    if text or usage or choice.get("finish_reason"):
                        yield ModelDelta(
                            text=text,
                            usage=usage,
                            finish_reason=choice.get("finish_reason"),
                        )
    except httpx.HTTPError as exc:
        # 网络/超时：流式期间不可透明重试（首段可能已输出），交终态处理。
        raise TaskError("GENERATION_UNREACHABLE", f"网络错误：{exc}", transient=True) from exc
