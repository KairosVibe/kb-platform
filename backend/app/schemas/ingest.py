"""导入受理相关 DTO（M03，F-03.01—F-03.06）。

字段与 FUNCTION-MAP §3 的输出契约**逐一对齐**，不多不少——多出的字段会让
前端把"契约外字段"当成承诺（AC-03.xx-03 一致性验收按输出契约核对）。

★ `progress` 在流水线（F-03.04）落地前恒为 null：契约允许 null 并规定
  "未知进度显示进行中"（AC-03.03-02），因此不为它编造 0 或 100。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- 请求


class RetryTaskRequest(BaseModel):
    """POST /api/index-tasks/{task_id}/retry（F-03.05 的 DTO 侧）。

    ★ `expected_revision` 走 body（API-CONTRACTS §1："修改 body 使用 expected_revision，
      首版统一 body"）；缺失由 pydantic 拒绝 → 422，与"缺失 422，不匹配 409"一致。
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


# ---------------------------------------------------------------- 响应


class UploadAcceptedOut(BaseModel):
    """F-03.01 输出：`{unit_id, task_id, status}`。"""

    model_config = ConfigDict(extra="forbid")

    unit_id: int = Field(gt=0)
    task_id: int = Field(gt=0)
    status: str


class BatchItemOut(BaseModel):
    """F-03.02 逐项结果：失败项 `unit_id`/`task_id` 为 null 并带 `error_code`。"""

    model_config = ConfigDict(extra="forbid")

    client_file_id: str
    relative_path: str
    unit_id: int | None = None
    task_id: int | None = None
    error_code: str | None = None


class BatchAcceptedOut(BaseModel):
    """F-03.02 输出：`{batch_id, items}`。"""

    model_config = ConfigDict(extra="forbid")

    batch_id: int = Field(gt=0)
    items: list[BatchItemOut]


class TaskStatusOut(BaseModel):
    """F-03.03 输出：`{status, stage, progress, error_code, attempts, revision}`。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    stage: str | None = None
    progress: int | None = None
    error_code: str | None = None
    attempts: int = Field(ge=0)
    revision: int = Field(ge=1)


class TaskRetriedOut(BaseModel):
    """F-03.05 输出：`{task_id, status}`。"""

    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(gt=0)
    status: str


class TaskRecoveredOut(BaseModel):
    """F-03.06 输出：`{requeued, superseded, failed}`。"""

    model_config = ConfigDict(extra="forbid")

    requeued: int = Field(ge=0)
    superseded: int = Field(ge=0)
    failed: int = Field(ge=0)
