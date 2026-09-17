"""M05 请求 DTO（API-CONTRACTS §8 F-05.01—F-05.09）。

响应由服务层 dict + `ok()` 包装；SSE 事件流是 text/event-stream，
不套 JSON 外壳（API-CONTRACTS §1），由路由直接返回 StreamingResponse。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    """F-05.01：POST /api/sessions。"""

    title: str | None = None


class MutateSessionRequest(BaseModel):
    """F-05.03：PATCH /api/sessions/{session_id}。"""

    action: str = Field(pattern="^(rename|delete)$")
    title: str | None = None


class AcceptQuestionRequest(BaseModel):
    """F-05.04：POST /api/chat/requests。"""

    session_id: int
    client_request_id: str = Field(min_length=8, max_length=64)
    question: str = Field(min_length=1)
