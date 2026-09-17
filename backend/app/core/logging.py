"""结构化日志（JSON 行格式）。

对应 FUNCTION-MAP.md §4（H01 之后的公共函数）、API-CONTRACTS.md §1（响应必须带 request_id）、
PRD.md §1.3（审计口径）。最小实现：不引入额外依赖，用标准库 logging 输出 JSON。

请求级 request_id 由中间件在请求入口生成并通过 contextvar 透传，保证一次请求的所有日志
可串联（排查线上问题的基础能力）。`ensure_request_id` 是响应层的兜底：即使中间件缺失，
响应里也不会出现空 request_id，且此后产生的日志会带上同一个 ID。
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

# 请求作用域上下文：由中间件设置，日志格式化时自动带上。
# 默认值 "-" 表示"尚未设置"，见 ensure_request_id。
_UNSET = "-"
request_id_var: ContextVar[str] = ContextVar("request_id", default=_UNSET)


def ensure_request_id() -> str:
    """返回当前请求的链路追踪 ID；未设置时生成一个并写回上下文。

    ★ 契约（FUNCTION-MAP §2 / API-CONTRACTS §1）：**每个响应都必须带 request_id**，
      且它与 `data.request_id`（问答业务的整数 ID）是不同字段，命名相同不可混用。

    正常情况下由中间件在请求入口设置；本函数是兜底——它把生成的 ID 写回 ContextVar，
    因此此后产生的日志会带同一个 ID，不会出现"响应里的 trace id 在日志中查不到"。
    """
    current = request_id_var.get()
    if current and current != _UNSET:
        return current
    generated = uuid.uuid4().hex
    request_id_var.set(generated)
    return generated


class JsonFormatter(logging.Formatter):
    """把日志记录序列化为单行 JSON。

    选择 JSON 而非纯文本的原因：便于后续被采集（ELK/Loki）与按字段检索；
    单行保证不会被多行堆栈打断采集管道。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 业务自定义字段（logger.info("...", extra={"extra_fields": {...}})）
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def setup_logging(level: str = "INFO") -> None:
    """初始化根日志器。幂等：重复调用不重复添加 handler。"""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # 降噪：这些库的 INFO 日志在正常请求下过于嘈杂
    for noisy in ("uvicorn.access", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
