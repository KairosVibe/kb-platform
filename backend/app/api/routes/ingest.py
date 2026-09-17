"""导入受理路由（M03：F-03.01 / F-03.02 / F-03.03 / F-03.05）。

对应 FUNCTION-MAP §2.2（薄路由契约）与 API-CONTRACTS §3（上传与幂等）。

三条路由层纪律（与 auth.py 同源）：
1. **只做 DTO 校验 + 服务调用 + 统一响应包装**；业务判断不写在这里。
2. **事务边界显式**：写操作用 `UnitOfWork.transaction()` 包住。
3. **`ctx` 不入 body**：当前身份来自 H01；`file:UploadFile` 在此拆为
   `filename + content` 传给服务（服务层不依赖 fastapi，与 `password` 的拆法一致）。

★ 状态码（API-CONTRACTS §1）：接受上传返回 **202**；同键同载荷的幂等复用返回 **200**
  和原 ID——响应体结构相同，前端据 HTTP 码区分"新建"与"复用"。
  `recover_tasks`（F-03.06）是内部调度入口，**没有** HTTP 路由（调用方"内部调度"）。
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_any_permission, require_permission
from app.core.response import BizError, ok
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.schemas.ingest import (
    BatchAcceptedOut,
    RetryTaskRequest,
    TaskRetriedOut,
    TaskStatusOut,
    UploadAcceptedOut,
)
from app.services import ingest_svc

router = APIRouter(tags=["文档导入"])


def _accepted_response(data: dict, model: type, *, created: bool = True) -> JSONResponse:
    """统一包装受理响应：新建 202、幂等复用 200（API-CONTRACTS §1）。"""
    code = 202 if created else 200
    return JSONResponse(status_code=code, content=ok(model(**data).model_dump()))


@router.post("/uploads", summary="单文件上传（F-03.01）")
async def accept_upload(
    file: UploadFile = File(...),
    category: str = Form(...),
    client_upload_id: UUID = Form(...),
    _perm: None = Depends(require_permission("kb:upload")),
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """受理单文件。幂等键 `client_upload_id` 映射到 `upload_item.client_file_id`。"""
    del _perm  # 权限已由依赖施加；参数形式只为把检查挂进路由前置
    content = await file.read()
    async with UnitOfWork(session).transaction():
        data, created = await ingest_svc.accept_upload(
            session,
            ctx_user_id=ctx.user_id,
            filename=file.filename or "",
            content=content,
            category=category,
            client_upload_id=client_upload_id,
        )
    return _accepted_response(data, UploadAcceptedOut, created=created)


@router.post("/upload-batches", summary="批量文件夹导入（F-03.02）")
async def accept_batch(
    files: list[UploadFile] = File(...),
    manifest: str = Form(...),
    _perm: None = Depends(require_permission("kb:upload")),
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """受理批量导入。

    multipart 两部分（API-CONTRACTS §3）：`files`（有序重复字段）与 `manifest`
    JSON（`client_batch_id` + `items[{client_file_id, relative_path, category}]`）。
    两者**长度与顺序严格匹配**，对不上直接 422——静默对齐会把"错位的结果"
    返回给错误的清单项。
    """
    try:
        manifest_data = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise BizError("INVALID_ARGUMENT", f"manifest 不是合法 JSON：{exc}") from exc
    if not isinstance(manifest_data, dict):
        raise BizError("INVALID_ARGUMENT", "manifest 必须是 JSON 对象")

    raw_client_batch_id = manifest_data.get("client_batch_id")
    items = manifest_data.get("items")
    if not isinstance(raw_client_batch_id, str) or not isinstance(items, list):
        raise BizError("INVALID_ARGUMENT", "manifest 缺少 client_batch_id 或 items")
    if len(items) != len(files):
        raise BizError(
            "INVALID_ARGUMENT",
            f"files 与 items 数量不一致：{len(files)} != {len(items)}",
        )

    try:
        client_batch_id = UUID(raw_client_batch_id)
        payloads: list[dict] = []
        for file, item in zip(files, items, strict=True):
            payloads.append(
                {
                    "filename": file.filename or "",
                    "client_file_id": UUID(str(item.get("client_file_id"))),
                    "relative_path": str(item.get("relative_path", "")),
                    "category": str(item.get("category", "")),
                    "content": await file.read(),
                }
            )
    except (KeyError, ValueError) as exc:
        raise BizError("INVALID_ARGUMENT", f"manifest 字段缺失或 UUID 不合法：{exc}") from exc

    async with UnitOfWork(session).transaction():
        data = await ingest_svc.accept_batch(
            session, ctx_user_id=ctx.user_id, payloads=payloads, client_batch_id=client_batch_id
        )
    return _accepted_response(data, BatchAcceptedOut)


@router.get("/index-tasks/{task_id}", summary="任务进度（F-03.03）")
async def get_task(
    task_id: int,
    _perm: None = Depends(require_any_permission("kb:view", "kb:upload")),
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """任务进度查询。`progress` 在流水线落地前恒为 null（AC-03.03-02"未知进度显示进行中"）。"""
    data = await ingest_svc.get_task(session, ctx_user_id=ctx.user_id, task_id=task_id)
    return JSONResponse(status_code=200, content=ok(TaskStatusOut(**data).model_dump()))


@router.post("/index-tasks/{task_id}/retry", summary="人工重试（F-03.05）")
async def retry_task(
    task_id: int,
    payload: RetryTaskRequest,
    _perm: None = Depends(require_permission("kb:edit")),
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """人工重试 `failed` 任务。不新建版本（AC-03.05-01），保留 attempts 历史。"""
    async with UnitOfWork(session).transaction():
        data = await ingest_svc.retry_task(
            session,
            ctx_user_id=ctx.user_id,
            task_id=task_id,
            expected_revision=payload.expected_revision,
        )
    return JSONResponse(status_code=200, content=ok(TaskRetriedOut(**data).model_dump()))
