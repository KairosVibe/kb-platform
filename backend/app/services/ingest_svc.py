"""M03 文档导入解析与任务（F-03.01/F-03.02/F-03.03/F-03.05/F-03.06）。

对应 FUNCTION-MAP §3「M03 文档导入解析与任务」、API-CONTRACTS §3（上传与幂等）、
PRD §1.2 第 2 条（任务状态机）。

★ 本模块**不含** F-03.04（索引流水线）：解析（H19/H20/H21）、向量化（H11）、
  版本写入与激活（H16/H17）尚未实现。因此本模块受理的任务会停在 `queued`，
  这是**如实状态**，不是缺陷——受理与执行本就是两个生命周期
  （AC-03.01-01 只要求"返回持久任务；上传后可查询"）。

三条贯穿全部函数的纪律：

1. **幂等靠数据库唯一键，不靠先查再插**。`upload_item(user_id, client_file_id)` 唯一
   （DATA-CONTRACTS §2），并发重复提交只有一个能插入成功；先查再插在并发下会
   双双通过，产生两个任务——正是 API-CONTRACTS §3 要防的。
2. **文件先落盘、后登记，登记失败回收文件**（F-03.01 处理逻辑第 4 条"失败回收
   临时文件"）。反过来（先登记后落盘）会让库里出现指向不存在文件的版本。
3. **任务层与 HTTP 层不互串**（FUNCTION-MAP §2）：批量受理中**单个文件**被拒
   （格式/大小/空文件）不改变 HTTP 200——批次请求本身成功了，失败原因随
   `items[].error_code` 返回；只有**整个批次**超限才 413。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import TaskError
from app.core.logging import ensure_request_id
from app.core.response import BizError
from app.db.base import utcnow
from app.db.repository import Repository, UnitOfWork, UniqueViolationError
from app.models import (
    IndexTask,
    KnowledgeUnit,
    KnowledgeVersion,
    UploadBatch,
    UploadItem,
)
from app.providers import embedding as embedding_provider
from app.tasks.lease import MAX_ATTEMPTS, TaskLease, renew_lease
from app.tasks.parsing import PARSER_VERSION, clean_text, parse_document, split_text
from app.tasks.version_store import activate_version, upsert_version

#: 批量上限（API-CONTRACTS §3："暂定100文件/200MiB"，为首版设计初值，可配置）。
BATCH_MAX_FILES = 100
BATCH_MAX_TOTAL_MB = 200

#: 瞬时失败后的重排延迟（F-03.04："瞬时错误retry_wait"；由 H14 到期重试）。
_RETRY_DELAY = timedelta(minutes=1)


# ---------------------------------------------------------------- 内部工具


def _payload_hash(content_sha256: str, category: str, relative_path: str) -> str:
    """受理幂等指纹：文件 SHA256 + 规范化字段（API-CONTRACTS §3）。

    ★ 同一个 `client_file_id` 配上**不同内容或不同分类**不是重放，而是键复用，
      必须区分（否则改一次分类就被当成重放而静默吞掉）。
    """
    material = "\x00".join((content_sha256, category.strip().casefold(), relative_path))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_content(filename: str, content: bytes) -> str:
    """扩展名/空文件/大小三项校验，返回小写扩展名（含点）。"""
    settings = get_settings()
    ext = Path(filename or "").suffix.lower()
    if ext not in settings.allowed_ext:
        raise BizError("UNSUPPORTED_FORMAT")
    if not content:
        raise BizError("EMPTY_FILE")
    if len(content) > settings.upload_max_mb * 1024 * 1024:
        raise BizError("FILE_TOO_LARGE")
    return ext


def _reject_unsafe_relative_path(relative_path: str) -> None:
    """相对路径只用于展示与分组（API-CONTRACTS §3）：拒绝绝对路径、盘符、`..` 与 NUL。

    ★ 校验的是**原始字符串**而不是规范化后的结果——"规范化之后再检查"会先把
      `a/../../etc` 折叠成 `etc`，攻击路径在检查前就消失了。
    """
    if not relative_path:
        return
    bad = (
        relative_path.startswith(("/", "\\"))
        or ":" in relative_path
        or "\0" in relative_path
        or ".." in relative_path
    )
    if bad:
        raise BizError("INVALID_ARGUMENT", f"相对路径不合法：{relative_path!r}")


def _store_file(content: bytes, ext: str) -> tuple[str, Path]:
    """把内容写到受控存储，返回 `(file_key, 绝对路径)`。文件名由**服务器生成**。"""
    settings = get_settings()
    directory = Path(settings.upload_dir)
    directory.mkdir(parents=True, exist_ok=True)
    file_key = f"{uuid4().hex}{ext}"
    path = directory / file_key
    path.write_bytes(content)
    return file_key, path


async def _register_unit_and_version(
    session: AsyncSession, *, ctx_user_id: int, filename: str, ext: str,
    category: str, file_key: str, content_sha256: str,
) -> tuple[KnowledgeUnit, KnowledgeVersion]:
    """登记知识单元（版本 1）与版本记录。ACL **默认全空**（AC-03.01-01）。"""
    settings = get_settings()
    title = Path(filename).stem.strip() or "未命名文档"
    unit = KnowledgeUnit(
        code=f"KB-{uuid4().hex[:10].upper()}",
        title=title[:100],
        format=ext.lstrip("."),
        category=category,
        creator_id=ctx_user_id,
    )
    session.add(unit)
    await session.flush()

    version = KnowledgeVersion(
        unit_id=int(unit.id),
        version=1,
        file_key=file_key,
        sha256=content_sha256,
        parser_version=PARSER_VERSION,
        chunk_config={"size": 500, "overlap": 50},
        embedding_model_version=settings.embedding_model_version,
        index_generation=settings.embedding_model_version,
    )
    session.add(version)
    await session.flush()
    return unit, version


def _log(
    session: AsyncSession, actor_id: int | None, action: str, resource_type: str,
    resource_id: int | None, before: dict[str, Any] | None, after: dict[str, Any] | None,
) -> None:
    """写操作日志（与业务变更同事务；DATA-CONTRACTS §3 第 1 条）。"""
    from app.models import OperationLog

    session.add(
        OperationLog(
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            trace_id=ensure_request_id(),
            before=before,
            after=after,
            status="ok",
        )
    )


# ---------------------------------------------------------------- F-03.01 / F-03.02


async def _accept_one(
    session: AsyncSession, *, ctx_user_id: int, filename: str, content: bytes,
    category: str, client_file_id: UUID, relative_path: str = "", batch_id: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """受理单个文件。返回 `(输出, 是否本次新建)`；假定调用方已开启事务。

    幂等三态（API-CONTRACTS §1"已存在且同载荷的幂等请求返回 200 和原 ID；同键不同载荷 409"）：
    键不存在 → 新建（created=True）；键存在且载荷一致 → 复用原 ID（created=False）；
    键存在但载荷不同 → 409 `IDEMPOTENCY_CONFLICT`。
    """
    ext = _validate_content(filename, content)
    category = category.strip()
    if not category or len(category) > 100:
        raise BizError("INVALID_ARGUMENT", "category 必须为 1—100 个字符")
    _reject_unsafe_relative_path(relative_path)

    content_sha256 = hashlib.sha256(content).hexdigest()
    fingerprint = _payload_hash(content_sha256, category, relative_path)

    # 幂等检查（唯一键兜底并发：两个并发同键请求只有一个能插入成功）
    existing = await Repository(session, UploadItem).find_one(
        {"user_id": ctx_user_id, "client_file_id": client_file_id}
    )
    if existing is not None:
        if existing.payload_hash != fingerprint:
            raise BizError("IDEMPOTENCY_CONFLICT", "该 client_file_id 已用于其他内容")
        return (
            {"unit_id": existing.unit_id, "task_id": existing.task_id, "status": "queued"},
            False,
        )

    file_key, path = _store_file(content, ext)
    try:
        unit, version = await _register_unit_and_version(
            session, ctx_user_id=ctx_user_id, filename=filename, ext=ext,
            category=category, file_key=file_key, content_sha256=content_sha256,
        )
        task = IndexTask(unit_id=int(unit.id), target_version=1)
        session.add(task)
        await session.flush()
        session.add(
            UploadItem(
                batch_id=batch_id,
                user_id=ctx_user_id,
                client_file_id=client_file_id,
                payload_hash=fingerprint,
                relative_path=relative_path or filename,
                unit_id=int(unit.id),
                task_id=int(task.id),
            )
        )
        await session.flush()
    except Exception:
        # 登记失败必须回收已落盘文件（F-03.01 处理逻辑第 4 条），
        # 否则存储目录会积累"没有任何库记录指向它"的孤儿文件。
        path.unlink(missing_ok=True)
        raise

    _log(
        session, ctx_user_id, "ingest.upload.accept", "knowledge_unit", int(unit.id),
        None, {"code": unit.code, "file_key": file_key, "sha256": content_sha256},
    )
    return {"unit_id": int(unit.id), "task_id": int(task.id), "status": task.status}, True


async def accept_upload(
    session: AsyncSession, *, ctx_user_id: int, filename: str, content: bytes,
    category: str, client_upload_id: UUID,
) -> tuple[dict[str, Any], bool]:
    """F-03.01：单文件受理。`file:UploadFile` 在路由层拆为 `filename + content`（同 ctx/ip 约定）。"""
    return await _accept_one(
        session, ctx_user_id=ctx_user_id, filename=filename, content=content,
        category=category, client_file_id=client_upload_id,
    )


async def accept_batch(
    session: AsyncSession, *, ctx_user_id: int, payloads: list[dict[str, Any]],
    client_batch_id: UUID,
) -> dict[str, Any]:
    """F-03.02：批量受理。`payloads` 为路由按 manifest 顺序对齐后的
    `[{filename, content, client_file_id, relative_path, category}]`。

    ★ 单项失败**不得**影响其他项（AC-03.02-01）：每项包在 SAVEPOINT 里，
      失败只回滚它自己的登记，批次继续。整批超限在进入循环前就拒绝（413）。
    """
    if len(payloads) > BATCH_MAX_FILES:
        raise BizError("INVALID_ARGUMENT", f"单批最多 {BATCH_MAX_FILES} 个文件")
    total = sum(len(p["content"]) for p in payloads)
    if total > BATCH_MAX_TOTAL_MB * 1024 * 1024:
        raise BizError("FILE_TOO_LARGE", f"批次总大小超过 {BATCH_MAX_TOTAL_MB}MiB 限制")

    # 批次载荷指纹：由各项的幂等键 + 内容长度 + 分类构成。它**不**替代逐项
    # payload_hash（单文件幂等仍按 item 判定），只用于识别"同键不同批次内容"。
    batch_fingerprint = hashlib.sha256(
        "\x00".join(
            f"{p['client_file_id']}:{len(p['content'])}:{p['category'].strip().casefold()}"
            for p in payloads
        ).encode("utf-8")
    ).hexdigest()

    batch = UploadBatch(
        user_id=ctx_user_id, client_batch_id=client_batch_id, payload_hash=batch_fingerprint
    )
    session.add(batch)
    try:
        await session.flush()
    except UniqueViolationError as exc:
        raise BizError("IDEMPOTENCY_CONFLICT", "该 client_batch_id 已用于其他批次") from exc

    results: list[dict[str, Any]] = []
    for item in payloads:
        entry: dict[str, Any] = {
            "client_file_id": str(item["client_file_id"]),
            "relative_path": item["relative_path"],
            "unit_id": None,
            "task_id": None,
            "error_code": None,
        }
        try:
            # SAVEPOINT：单项失败只回滚它自己（UnitOfWork 的外层事务不受影响）。
            async with session.begin_nested():
                accepted, _created = await _accept_one(
                    session, ctx_user_id=ctx_user_id, filename=item["filename"],
                    content=item["content"], category=item["category"],
                    client_file_id=item["client_file_id"],
                    relative_path=item["relative_path"], batch_id=int(batch.id),
                )
            entry["unit_id"] = accepted["unit_id"]
            entry["task_id"] = accepted["task_id"]
        except BizError as exc:
            entry["error_code"] = exc.code
        results.append(entry)

    return {"batch_id": int(batch.id), "items": results}


# ---------------------------------------------------------------- F-03.03


async def get_task(session: AsyncSession, *, ctx_user_id: int, task_id: int) -> dict[str, Any]:
    """F-03.03：任务进度查询。

    ★ 归属核验用**创建者比对**，不匹配返回 404 `NOT_FOUND` 而不是 403——
      与全站"按对象防枚举"口径一致，不向无权者披露任务是否存在。
    """
    row = (
        (
            await session.execute(
                select(IndexTask, KnowledgeUnit)
                .join(KnowledgeUnit, KnowledgeUnit.id == IndexTask.unit_id)
                .where(IndexTask.id == task_id)
            )
        )
        .first()
    )
    if row is None:
        raise BizError("NOT_FOUND")
    task, unit = row
    if int(unit.creator_id) != ctx_user_id:
        raise BizError("NOT_FOUND")

    return {
        "status": task.status,
        "stage": task.stage,
        # progress 在 H16/H17（流水线）落地前恒为 null——"未知进度显示进行中"（AC-03.03-02），
        # 前端以 status 为准，不得把 null 解释成 0%。
        "progress": None,
        "error_code": task.error_code,
        "attempts": int(task.attempts),
        "revision": int(task.revision),
    }


# ---------------------------------------------------------------- F-03.05


async def retry_task(
    session: AsyncSession, *, ctx_user_id: int, task_id: int, expected_revision: int
) -> dict[str, Any]:
    """F-03.05：人工重试。只允许 `failed`（"只允许失败当前版本"）。

    ★ 重试**不新建版本**（AC-03.05-01"重复操作不建重复版本"）：`target_version`
      保持不变，仅把状态复位为 `queued` 并保留 `attempts` 历史——重试次数是
      诊断依据，清零等于掩盖"这个任务已经连续失败了几次"。
    """
    task = await Repository(session, IndexTask).get(task_id, for_update=True)
    if task is None:
        raise BizError("NOT_FOUND")
    unit = await Repository(session, KnowledgeUnit).get(int(task.unit_id))
    if unit is None or int(unit.creator_id) != ctx_user_id:
        raise BizError("NOT_FOUND")
    if int(task.revision) != expected_revision:
        raise BizError("REVISION_CONFLICT")
    if task.status != "failed":
        raise BizError(
            "TASK_STATE_CONFLICT", f"仅 failed 状态可人工重试，当前为 {task.status}"
        )

    before = {"status": task.status, "attempts": int(task.attempts)}
    updated = await Repository(session, IndexTask).update_if(
        task_id,
        expected={"id": task_id, "revision": expected_revision, "status": "failed"},
        patch={
            "status": "queued",
            "error_code": None,
            "stage": None,
            "lease_token": None,
            "lease_until": None,
            "worker_id": None,
            "next_retry_at": None,
            "revision": expected_revision + 1,
        },
    )
    if not updated:
        raise BizError("REVISION_CONFLICT")

    _log(
        session, ctx_user_id, "ingest.task.retry", "index_task", task_id,
        before, {"status": "queued"},
    )
    return {"task_id": task_id, "status": "queued"}


# ---------------------------------------------------------------- F-03.06


async def recover_tasks(
    session: AsyncSession, *, now: datetime, batch_size: int = 100
) -> dict[str, int]:
    """F-03.06：任务恢复补偿（内部调度调用，无人类操作者）。

    三类收敛，各自是**单条条件 UPDATE**，行数即返回计数：
    1. 租约过期的 `running`：尝试次数已到上限 → `failed`（"临时失败最多5次"，
       永久错误不循环）；未到上限 → `retry_wait`（requeued）。
    2. 目标版本已落后于单元当前版本的 queued/retry_wait/running → `superseded`
       （"旧任务superseded"；它不是失败，不得计入失败率）。

    ★ 不在这里"条件领取并执行"——领取是执行器的职责（H14），本函数只做
      状态收敛。把两者合在一起会让"恢复"与"执行"争抢同一批行。
    """
    _ = batch_size  # D1 规模下单条 UPDATE 即可；引入逐批领取时再启用
    requeued = 0
    superseded = 0
    failed = 0

    expired = (
        IndexTask.status == "running",
        IndexTask.lease_until.is_not(None),
        IndexTask.lease_until < now,
    )

    result = await session.execute(
        update(IndexTask)
        .where(*expired, IndexTask.attempts >= MAX_ATTEMPTS)
        .values(
            status="failed", error_code="EXECUTOR_LOST", stage=None,
            lease_token=None, lease_until=None, worker_id=None,
            next_retry_at=None, revision=IndexTask.revision + 1,
        )
    )
    failed = int(result.rowcount or 0)

    result = await session.execute(
        update(IndexTask)
        .where(*expired, IndexTask.attempts < MAX_ATTEMPTS)
        .values(
            status="retry_wait", stage=None, lease_token=None, lease_until=None,
            worker_id=None, next_retry_at=now, revision=IndexTask.revision + 1,
        )
    )
    requeued = int(result.rowcount or 0)

    stale_targets = (
        IndexTask.status.in_(("queued", "retry_wait", "running")),
        IndexTask.unit_id.in_(
            select(KnowledgeUnit.id).where(
                KnowledgeUnit.content_version > IndexTask.target_version
            )
        ),
    )
    result = await session.execute(
        update(IndexTask)
        .where(*stale_targets)
        .values(
            status="superseded", stage=None, lease_token=None, lease_until=None,
            worker_id=None, next_retry_at=None, revision=IndexTask.revision + 1,
        )
    )
    superseded = int(result.rowcount or 0)

    if failed or requeued or superseded:
        _log(
            session, None, "ingest.task.recover", "index_task", None,
            None, {"requeued": requeued, "superseded": superseded, "failed": failed},
        )
    return {"requeued": requeued, "superseded": superseded, "failed": failed}


# ---------------------------------------------------------------- F-03.04


async def _set_stage(session: AsyncSession, lease: TaskLease, stage: str) -> None:
    """阶段推进（短事务、条件于租约）。stage 与 status 正交（PRD §1.2 第 2 条）。"""
    async with UnitOfWork(session).transaction():
        await session.execute(
            update(IndexTask)
            .where(
                IndexTask.id == lease.task_id,
                IndexTask.lease_token == lease.lease_token,
                IndexTask.status == "running",
            )
            .values(stage=stage)
        )


async def _terminate(
    session: AsyncSession, lease: TaskLease, status: str, error_code: str, settings: Any
) -> dict[str, Any]:
    """以**条件更新**收敛任务终态。

    ★ 条件里带 `lease_token`：若租约已易主（recover 回收后另有人领取），行数=0，
      什么都不写——**不持有租约的一方无权写任务状态**（H15"失败停止写入和提交"）。
    """
    values: dict[str, Any] = {
        "status": status,
        "error_code": error_code,
        "stage": None,
        "lease_token": None,
        "lease_until": None,
        "worker_id": None,
        "revision": IndexTask.revision + 1,
    }
    values["next_retry_at"] = (
        utcnow() + _RETRY_DELAY if status == "retry_wait" else None
    )
    async with UnitOfWork(session).transaction():
        result = await session.execute(
            update(IndexTask)
            .where(
                IndexTask.id == lease.task_id,
                IndexTask.lease_token == lease.lease_token,
                IndexTask.status == "running",
            )
            .values(**values)
        )
        applied = result.rowcount == 1
    if applied:
        _log(
            session, None, f"ingest.pipeline.{status}", "index_task", lease.task_id,
            None, {"error_code": error_code},
        )
    return {"status": status, "indexed_version": None, "error_code": error_code}


async def run_pipeline(session: AsyncSession, *, task_id: int, lease_token: UUID) -> dict[str, Any]:
    """F-03.04：执行索引任务（H19→H20→H21→H11→H16→H17，中途 H15 续租）。

    ★ 调用方**不得**把本函数包进外层大事务：续租与阶段推进必须即时可见，
      否则双 worker 防护失效（见 version_store 模块注释）。本函数自开短事务。

    ★ 所有终态写入都**条件于租约**——任何时候发现租约不属于自己，立即停止，
      不写任何状态。瞬时错误 → retry_wait（H14 到期重试）；永久错误 → failed。
    """
    settings = get_settings()
    task = await Repository(session, IndexTask).get(task_id)
    if task is None:
        raise TaskError("TASK_MISSING", "任务不存在", transient=False)
    if task.status != "running" or task.lease_token != lease_token:
        raise TaskError("LEASE_INVALID", "租约不匹配或已失效", transient=True)

    lease = TaskLease(
        task_id=task_id,
        lease_token=lease_token,
        lease_until=task.lease_until,
        worker_id=task.worker_id or "pipeline",
        target_version=int(task.target_version),
    )
    unit = await Repository(session, KnowledgeUnit).get(int(task.unit_id))
    if unit is None or unit.is_deleted:
        return await _terminate(session, lease, "superseded", "TARGET_GONE", settings)
    if int(unit.content_version) != int(task.target_version):
        return await _terminate(session, lease, "superseded", "SUPERSEDED", settings)

    version_row = (
        await session.execute(
            select(KnowledgeVersion).where(
                KnowledgeVersion.unit_id == int(task.unit_id),
                KnowledgeVersion.version == int(task.target_version),
            )
        )
    ).scalars().first()
    if version_row is None:
        return await _terminate(
            session, lease, "failed", "VERSION_RECORD_MISSING", settings
        )

    warnings: list[str] = []
    try:
        # ---- 解析 / 清洗 / 切片（H19→H20→H21，纯计算）----
        await _set_stage(session, lease, "parsing")
        path = Path(settings.upload_dir) / version_row.file_key
        raw_text, locations = parse_document(path, unit.format)
        cleaned, locations, warnings = clean_text(raw_text, locations)
        chunk_config = version_row.chunk_config or {}
        chunks = split_text(
            cleaned,
            locations,
            int(chunk_config.get("size", 500)),
            int(chunk_config.get("overlap", 50)),
        )
        if not chunks:
            raise TaskError("PARSE_EMPTY", "清洗后没有可索引切片", transient=False)

        # ---- 向量化（H11，外部副作用）----
        if not await renew_lease(
            session, task_id=task_id, lease_token=lease_token, now=utcnow()
        ):
            raise TaskError("LEASE_LOST", "租约已易主，停止写入", transient=True)
        await _set_stage(session, lease, "embedding")
        texts = [str(chunk["text"]) for chunk in chunks]
        vectors, _usage, model_version = await embedding_provider.embed_batches(texts, settings)
        if model_version != version_row.embedding_model_version:
            # ★ 比较对象必须是**版本记录**里声明的空间标识，而不是当前配置——
            #   后者与 H11 同源（恒等，是无效守卫）。真正要拦的是"这次算出来的向量
            #   与建版本时声明的不属同一空间"，那会污染整个 collection（PRD BC-09.02）。
            raise TaskError(
                "EMBED_MODEL_DRIFT",
                f"本次向量化空间标识 {model_version!r} != 版本记录 "
                f"{version_row.embedding_model_version!r}，必须换代际重建",
                transient=False,
            )

        # ---- 持久化与向量写入（H16，外部副作用 + 短事务）----
        if not await renew_lease(
            session, task_id=task_id, lease_token=lease_token, now=utcnow()
        ):
            raise TaskError("LEASE_LOST", "租约已易主，停止写入", transient=True)
        await _set_stage(session, lease, "indexing")
        expected, written, verified = await upsert_version(
            session,
            unit_id=int(task.unit_id),
            version=int(task.target_version),
            chunks=[{**chunk, "vector": vector} for chunk, vector in zip(chunks, vectors)],
            lease=lease,
            config=settings,
        )
        if not verified:
            raise TaskError(
                "VECTOR_WRITE_MISMATCH",
                f"向量核对失败：期望 {expected}，写入 {written}",
                transient=True,
            )
    except TaskError as exc:
        # 瞬时 → retry_wait（保留 attempts 历史，H14 到期重试）；永久 → failed。
        return await _terminate(
            session, lease, "retry_wait" if exc.transient else "failed", exc.code, settings
        )

    activated, reason = await activate_version(
        session,
        unit_id=int(task.unit_id),
        version=int(task.target_version),
        lease=lease,
    )
    if not activated:
        return await _terminate(
            session,
            lease,
            "superseded" if reason == "superseded" else "failed",
            reason.upper(),
            settings,
        )

    _log(
        session, None, "ingest.pipeline.succeeded", "index_task", task_id,
        None,
        {
            "indexed_version": int(task.target_version),
            "chunks": len(chunks),
            "warnings": warnings,
        },
    )
    return {
        "status": "succeeded",
        "indexed_version": int(task.target_version),
        "error_code": None,
    }


__all__ = [
    "accept_batch",
    "accept_upload",
    "get_task",
    "recover_tasks",
    "retry_task",
    "run_pipeline",
]
