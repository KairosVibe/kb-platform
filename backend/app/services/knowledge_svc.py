"""知识生命周期与四维权限（M04，FUNCTION-MAP §3 F-04.01—F-04.09）。

三条贯穿本模块的纪律：

1. **数据读权与功能权限分开**（API-CONTRACTS §1）：功能检查（kb:view/kb:edit/…）
   由路由依赖 H02 完成；**正文级**访问还要过 H04 数据读权。数据读权不过时
   返回 **404 NOT_FOUND**（防枚举："资源不存在或无权访问"），**不是 403**——
   403 会披露"对象存在但你无权"，正好违反防枚举规则。
2. **版本只增不改**：替换文档/切片编辑都生成 `content_version+1` 的新版本，
   旧版本切片**原地不动**（"不得就地重排旧版本序号"）。历史引用
   （`message_source` 指向旧版本 chunk_id）因此永远有效。
3. **切片编辑的源文件以文本形态重存**：编辑只发生在切片上，而流水线 F-03.04
   从源文件重新解析——若不把编辑后的文本写回源文件，重索引会**静默抹掉人工编辑**
   （这是比"解析失败"更危险的缺陷：不报错、悄悄回滚用户操作）。因此新版本的
   `file_key` 是重组文本的 `.txt` 文件，解析格式按**文件扩展名**取（run_pipeline
   已改为以扩展名为权威）。
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.response import BizError
from app.db.repository import Repository, UnitOfWork
from app.engines.permission import UserCtx, authorize_units
from app.models import (
    Chunk,
    CleanupTask,
    Department,
    IndexTask,
    KnowledgeAclDepartment,
    KnowledgeAclRole,
    KnowledgeAclUser,
    KnowledgeUnit,
    KnowledgeVersion,
    Role,
    User,
)
from app.services.ingest_svc import (  # 服务包内助手复用（不对外暴露）
    PARSER_VERSION,
    _log,
    _store_file,
    _validate_content,
)
from app.tasks.parsing import estimate_tokens

#: 分页上限（API-CONTRACTS §1：page=1,size=20，上限 100）。
_PAGE_MAX = 100
#: ACL 数组上限（API-CONTRACTS §1：数组去重后最多 1000 项）。
_ACL_MAX = 1000

_ACL_MODELS = (KnowledgeAclDepartment, KnowledgeAclRole, KnowledgeAclUser)


# ---------------------------------------------------------------- 内部工具


def _check_paging(page: int, size: int) -> None:
    if page < 1 or size < 1 or size > _PAGE_MAX:
        raise BizError("INVALID_ARGUMENT", f"分页参数不合法：page={page}, size={size}")


def _norm_q(q: str | None) -> str:
    value = (q or "").strip()
    if len(value) > 200:
        raise BizError("INVALID_ARGUMENT", "q 最长 200 字符")
    return value


async def _require_unit(session: AsyncSession, unit_id: int) -> KnowledgeUnit:
    """台账存在性（未删除）。已删除与不存在同样 404——防枚举不区分。"""
    unit = await Repository(session, KnowledgeUnit).get(unit_id)
    if unit is None or unit.is_deleted:
        raise BizError("NOT_FOUND")
    return unit


async def _require_readable(session: AsyncSession, ctx: UserCtx, unit_id: int) -> KnowledgeUnit:
    """H04 数据读权检查 + 存在性。无权与不存在**同样 404**（防枚举）。

    ★ 判定用 H04 的 `denied`/`versions`，**不用** `allowed`：`allowed` 含索引
      可用性（is_retrievable），而"读切片"只需要数据读权——索引未就绪的单元
      （pending）依然可以查看其台账与已有切片，只是检索不到（那是 M05 的事）。
    """
    _allowed, denied, _unavailable, versions = await authorize_units(session, ctx, [unit_id])
    if unit_id not in versions or unit_id in denied:
        raise BizError("NOT_FOUND")
    unit = await Repository(session, KnowledgeUnit).get(unit_id)
    assert unit is not None
    return unit


async def _acl_maps(
    session: AsyncSession, unit_ids: list[int]
) -> tuple[dict[int, list[int]], dict[int, list[int]], dict[int, list[int]]]:
    """批量取三张 ACL 表（每表一次 IN 查询），供台账 acl_tags 与 get_acl 使用。"""
    if not unit_ids:
        return {}, {}, {}
    maps: tuple[dict[int, list[int]], dict[int, list[int]], dict[int, list[int]]] = (
        {},
        {},
        {},
    )
    for model, target in zip(_ACL_MODELS, maps, strict=True):
        rows = (
            await session.execute(
                select(model.unit_id, model.subject_id).where(model.unit_id.in_(unit_ids))
            )
        ).all()
        for unit_id, subject_id in rows:
            target.setdefault(int(unit_id), []).append(int(subject_id))
    for target in maps:
        for ids in target.values():
            ids.sort()
    return maps


async def build_acl_tags(
    session: AsyncSession, units: list[KnowledgeUnit]
) -> dict[int, list[str]]:
    """台账 `acl_tags`：`global` / `dept:3` / `role:5` / `user:7`，global 优先、各维按 ID 升序。

    用 ID 前缀而不是名称：避免台账页为拼标签做三张表的名称联查
    （前端有实体缓存时自行映射名称）。
    """
    """台账 `acl_tags`：global 优先，其后 dept/role/user 按 ID 升序。"""
    ids = [int(u.id) for u in units]
    depts, roles, users = await _acl_maps(session, ids)
    result: dict[int, list[str]] = {}
    for unit in units:
        unit_id = int(unit.id)
        items: list[str] = []
        if unit.is_global:
            items.append("global")
        items.extend(f"dept:{v}" for v in depts.get(unit_id, ()))
        items.extend(f"role:{v}" for v in roles.get(unit_id, ()))
        items.extend(f"user:{v}" for v in users.get(unit_id, ()))
        result[unit_id] = items
    return result


async def _revision_guard(
    session: AsyncSession, *, unit_id: int, expected_revision: int, **values: Any
) -> int:
    """条件更新（乐观锁）。返回新 revision；冲突 409。

    ★ 所有管理写操作都走这里：`WHERE revision = expected` 的条件更新，
      rowcount=1 才算成功——"先查后写"的两次访问之间存在窗口，只有条件更新
      能挡住并发改写。
    """
    result = await session.execute(
        update(KnowledgeUnit)
        .where(
            KnowledgeUnit.id == unit_id,
            KnowledgeUnit.revision == expected_revision,
            KnowledgeUnit.is_deleted.is_(False),
        )
        .values(revision=KnowledgeUnit.revision + 1, **values)
    )
    if result.rowcount != 1:
        raise BizError("REVISION_CONFLICT")
    return expected_revision + 1


def _validate_id_list(values: list[int] | None, label: str) -> list[int]:
    """ACL 数组归一：去重、上限 1000；非法元素**报错而不静默丢弃**（API-CONTRACTS §1）。"""
    if values is None:
        return []
    normalized: list[int] = []
    for value in values:
        try:
            normalized.append(int(value))
        except (TypeError, ValueError) as exc:
            raise BizError("INVALID_ARGUMENT", f"{label} 含非法 ID：{value!r}") from exc
        if normalized[-1] <= 0:
            raise BizError("INVALID_ARGUMENT", f"{label} 含非法 ID：{value!r}")
    unique = sorted(set(normalized))
    if len(unique) > _ACL_MAX:
        raise BizError("INVALID_ARGUMENT", f"{label} 去重后超过 {_ACL_MAX} 项")
    return unique


async def _require_entities_exist(
    session: AsyncSession, model: type, ids: list[int], label: str, *, require_enabled: bool = False
) -> list[int]:
    """校验授权对象存在（用户还须启用）。缺失/停用 422 `ACL_ENTITY_INVALID`。"""
    if not ids:
        return []
    query = select(model.id).where(model.id.in_(ids))
    if require_enabled:
        query = query.where(model.enabled.is_(True))  # type: ignore[attr-defined]
    found = {int(row[0]) for row in (await session.execute(query)).all()}
    missing = [value for value in ids if value not in found]
    if missing:
        raise BizError("ACL_ENTITY_INVALID", f"{label} 不存在或已停用：{missing[:5]}")
    return ids


# ---------------------------------------------------------------- F-04.01


async def list_units(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    q: str = "",
    category: str | None = None,
    enabled: bool | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """F-04.01：台账查询（kb:view）。只查元数据，不查正文（kb:view 不扩正文读权）。"""
    _check_paging(page, size)
    q = _norm_q(q)
    if category is not None:
        category = category.strip() or None
        if category and len(category) > 100:
            raise BizError("INVALID_ARGUMENT", "category 最长 100 字符")

    conditions = [KnowledgeUnit.is_deleted.is_(False)]
    if q:
        conditions.append(
            KnowledgeUnit.title.like(f"%{q}%") | KnowledgeUnit.code.like(f"%{q}%")
        )
    if category:
        conditions.append(KnowledgeUnit.category == category)
    if enabled is not None:
        conditions.append(KnowledgeUnit.enabled == bool(enabled))

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(KnowledgeUnit).where(*conditions)
            )
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(KnowledgeUnit)
                .where(*conditions)
                .order_by(KnowledgeUnit.created_at.desc(), KnowledgeUnit.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    tags = await build_acl_tags(session, list(rows))
    items = [
        {
            "id": int(unit.id),
            "code": unit.code,
            "title": unit.title,
            "format": unit.format,
            "category": unit.category,
            "acl_tags": tags.get(int(unit.id), []),
            "updated_at": unit.updated_at,
            "enabled": bool(unit.enabled),
            "index_status": unit.index_status,
            "revision": int(unit.revision),
        }
        for unit in rows
    ]
    return {"items": items, "total": total}


# ---------------------------------------------------------------- F-04.02


async def update_metadata(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    unit_id: int,
    title: str,
    category: str,
    expected_revision: int,
) -> dict[str, Any]:
    """F-04.02：修改标题与分类（kb:edit）。空标题 422；并发 409。"""
    unit = await _require_unit(session, unit_id)
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise BizError("INVALID_ARGUMENT", "title 必须为 1—200 个字符")
    category = (category or "").strip()
    if not category or len(category) > 100:
        raise BizError("INVALID_ARGUMENT", "category 必须为 1—100 个字符")

    before = {"title": unit.title, "category": unit.category}
    new_revision = await _revision_guard(
        session, unit_id=unit_id, expected_revision=expected_revision, title=title, category=category
    )
    _log(
        session, ctx.user_id, "knowledge.update_metadata", "knowledge_unit", unit_id,
        before, {"title": title, "category": category},
    )
    return {"unit_id": unit_id, "revision": new_revision}


# ---------------------------------------------------------------- F-04.03


async def read_chunks(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    unit_id: int,
    version: int | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """F-04.03：查看正文切片（kb:view + 数据读权）。`version=None` → 当前内容版本。"""
    _check_paging(page, size)
    unit = await _require_readable(session, ctx, unit_id)
    ver = int(version) if version is not None else int(unit.content_version)
    version_row = (
        await session.execute(
            select(KnowledgeVersion).where(
                KnowledgeVersion.unit_id == unit_id, KnowledgeVersion.version == ver
            )
        )
    ).scalars().first()
    if version_row is None:
        raise BizError("NOT_FOUND")

    conditions = (Chunk.unit_id == unit_id, Chunk.version == ver)
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(Chunk).where(*conditions)
            )
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(Chunk)
                .where(*conditions)
                .order_by(Chunk.seq.asc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "chunk_id": int(row.id),
            "version": ver,
            "seq": int(row.seq),
            "text": row.text,
            "page_no": row.location.get("page_no"),
            "offset": row.location.get("start_offset"),
        }
        for row in rows
    ]
    return {"items": items, "total": total}


# ---------------------------------------------------------------- F-04.04


async def replace_document(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    unit_id: int,
    filename: str,
    content: bytes,
    expected_revision: int,
) -> dict[str, Any]:
    """F-04.04：替换文档（kb:edit + 数据读权）。生成新版本并登记索引任务。

    - ACL 三张表挂在 unit_id 上，与版本无关 → **天然保留**，无需搬运；
    - 旧版本任务不用取消：F-03.04 激活前的 `superseded` 前置判定会让它自然收敛；
    - `index_status`：已索引过的置 `stale`（内容已领先索引），从未索引过的保持 `pending`。
    """
    unit = await _require_readable(session, ctx, unit_id)
    if int(unit.revision) != int(expected_revision):
        # 先查版本再落盘：避免冲突时白写一份文件。
        raise BizError("REVISION_CONFLICT")
    ext = _validate_content(filename, content)
    settings = get_settings()

    previous = (
        await session.execute(
            select(KnowledgeVersion).where(
                KnowledgeVersion.unit_id == unit_id,
                KnowledgeVersion.version == int(unit.content_version),
            )
        )
    ).scalars().first()
    chunk_config = dict(previous.chunk_config) if previous else {"size": 500, "overlap": 50}

    content_sha256 = hashlib.sha256(content).hexdigest()
    file_key, _path = _store_file(content, ext)
    new_version = int(unit.content_version) + 1

    new_revision = await _revision_guard(
        session,
        unit_id=unit_id,
        expected_revision=expected_revision,
        content_version=new_version,
        format=ext.lstrip("."),
        index_status="stale" if unit.index_status == "indexed" else "pending",
    )
    version_row = KnowledgeVersion(
        unit_id=unit_id,
        version=new_version,
        file_key=file_key,
        sha256=content_sha256,
        parser_version=PARSER_VERSION,
        chunk_config=chunk_config,
        embedding_model_version=settings.embedding_model_version,
        index_generation=settings.embedding_model_version,
    )
    session.add(version_row)
    await session.flush()
    task = IndexTask(unit_id=unit_id, target_version=new_version)
    session.add(task)
    await session.flush()

    # F-06.07（来源失效）属 M06，FAQ 组件未建——此处无动作可做；
    # FAQ 命中时逐项"当前来源授权与状态复核"（H06 语义）保证不会读到已替换内容。
    _log(
        session, ctx.user_id, "knowledge.replace_document", "knowledge_unit", unit_id,
        {"content_version": int(unit.content_version), "format": unit.format},
        {"content_version": new_version, "format": ext.lstrip("."), "task_id": int(task.id)},
    )
    return {"task_id": int(task.id), "target_version": new_version}


# ---------------------------------------------------------------- F-04.05


async def mutate_chunks(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    unit_id: int,
    chunk_id: int,
    action: str,
    text: str | None = None,
    split_offset: int | None = None,
    expected_revision: int,
) -> dict[str, Any]:
    """F-04.05：切片编辑/拆分/删除（kb:edit + 数据读权）。

    实现顺序对应契约：授权 → 新版本 → 按 chunk_id 修改 → 新版本重排 seq → 登记任务。
    人工编辑后的全文以 `.txt` 重存为新版本源文件（模块注释解释了为什么必须这样做），
    流水线重新解析时 H16 按 `(unit,version,seq)` 复用稳定 id、仅更新变化的文本。
    """
    unit = await _require_readable(session, ctx, unit_id)
    if int(unit.revision) != int(expected_revision):
        raise BizError("REVISION_CONFLICT")
    current = int(unit.content_version)

    rows = (
        (
            await session.execute(
                select(Chunk)
                .where(Chunk.unit_id == unit_id, Chunk.version == current)
                .order_by(Chunk.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    index = next((i for i, row in enumerate(rows) if int(row.id) == int(chunk_id)), None)
    if index is None:
        raise BizError("NOT_FOUND")

    texts = [row.text for row in rows]
    if action == "edit":
        edited = (text or "").strip()
        if not edited:
            raise BizError("INVALID_ARGUMENT", "编辑后的切片内容不能为空")
        texts[index] = edited
    elif action == "split":
        source = texts[index]
        if split_offset is None or not 0 < split_offset < len(source):
            raise BizError(
                "INVALID_ARGUMENT",
                f"split_offset 必须在 1—{len(source) - 1} 之间（空拆分或越界均为 422）",
            )
        texts[index: index + 1] = [source[:split_offset], source[split_offset:]]
    elif action == "delete":
        if len(texts) == 1:
            # 删掉唯一切片会得到空文档：流水线必然以 PARSE_EMPTY 永久失败，
            # 与其让任务失败不如在入口拦下（空文档不是合法的知识版本）。
            raise BizError("INVALID_ARGUMENT", "不能删除唯一切片；请改用知识删除")
        del texts[index]
    else:
        raise BizError("INVALID_ARGUMENT", f"不支持的操作：{action!r}（edit/split/delete）")

    new_version = current + 1
    merged = "\n\n".join(texts)
    settings = get_settings()
    file_key, _path = _store_file(merged.encode("utf-8"), ".txt")

    previous = (
        await session.execute(
            select(KnowledgeVersion).where(
                KnowledgeVersion.unit_id == unit_id,
                KnowledgeVersion.version == current,
            )
        )
    ).scalars().first()
    chunk_config = dict(previous.chunk_config) if previous else {"size": 500, "overlap": 50}

    new_revision = await _revision_guard(
        session,
        unit_id=unit_id,
        expected_revision=expected_revision,
        content_version=new_version,
        index_status="pending",
    )
    session.add(
        KnowledgeVersion(
            unit_id=unit_id,
            version=new_version,
            file_key=file_key,
            sha256=hashlib.sha256(merged.encode("utf-8")).hexdigest(),
            parser_version=PARSER_VERSION,
            chunk_config=chunk_config,
            embedding_model_version=settings.embedding_model_version,
            index_generation=settings.embedding_model_version,
        )
    )
    # ★ 必须显式 flush：Chunk 的复合 FK 指向 knowledge_version，但两个模型之间
    #   没有 relationship，Unit of Work 无法推断插入顺序——实测切片行先于版本行
    #   执行 INSERT，直接违反外键约束。
    await session.flush()
    # 预建新版本切片行（seq 重排为 0..n-1，文档顺序）。定位基于重组文本的码点偏移，
    # original_offset=None——重组文本与原文的映射已不可靠，不伪造精确位置。
    cursor = 0
    for seq, chunk_text in enumerate(texts):
        session.add(
            Chunk(
                unit_id=unit_id,
                version=new_version,
                seq=seq,
                text=chunk_text,
                token_count=estimate_tokens(chunk_text),
                location={
                    "page_no": None,
                    "start_offset": cursor,
                    "end_offset": cursor + len(chunk_text),
                    "original_offset": None,
                },
            )
        )
        cursor += len(chunk_text) + 2  # 与重组时的 "\n\n" 连接符保持一致

    task = IndexTask(unit_id=unit_id, target_version=new_version)
    session.add(task)
    await session.flush()

    _log(
        session, ctx.user_id, f"knowledge.chunk_{action}", "knowledge_unit", unit_id,
        {"content_version": current, "chunk_id": int(chunk_id)},
        {"content_version": new_version, "chunks": len(texts), "task_id": int(task.id)},
    )
    return {"task_id": int(task.id), "target_version": new_version}


# ---------------------------------------------------------------- F-04.06


async def set_enabled(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    unit_id: int,
    enabled: bool,
    expected_revision: int,
) -> dict[str, Any]:
    """F-04.06：知识启停（kb:edit）。

    ★ "重新启用仍须有效索引"：`index_status != indexed` 时启用是**状态冲突**（409）——
      启用一个没有可用索引的知识只会制造"看得到台账、查不到内容"的困惑态。
      停用始终允许（停用本身就是把内容从检索里摘除的手段）。
    """
    unit = await _require_unit(session, unit_id)
    if enabled and unit.index_status != "indexed":
        raise BizError("INDEX_NOT_READY", f"当前索引状态为 {unit.index_status}，不能启用")
    before = {"enabled": bool(unit.enabled)}
    new_revision = await _revision_guard(
        session, unit_id=unit_id, expected_revision=expected_revision, enabled=bool(enabled)
    )
    # F-06.07（启停强制复核 FAQ 来源）属 M06，组件未建——无动作可做；
    # 活动流失效同理（通知组件属后续里程碑）。
    _log(
        session, ctx.user_id, "knowledge.set_enabled", "knowledge_unit", unit_id,
        before, {"enabled": bool(enabled)},
    )
    return {"unit_id": unit_id, "enabled": bool(enabled), "revision": new_revision}


# ---------------------------------------------------------------- F-04.07


async def delete_unit(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    unit_id: int,
    expected_revision: int,
) -> dict[str, Any]:
    """F-04.07：知识删除（kb:delete）。墓碑 + 清理任务**同事务**。

    幂等：对已删除单元重复提交返回既有清理结果（不产生第二个清理任务——
    以"最新一条任务"为准返回其状态）。物理清理（文件/向量，H18）由清理执行器
    异步完成，本函数只落墓碑与任务；清理只删派生物，**不复活单元**。
    """
    unit = await Repository(session, KnowledgeUnit).get(unit_id)
    if unit is None:
        raise BizError("NOT_FOUND")
    if unit.is_deleted:
        existing = (
            await session.execute(
                select(CleanupTask)
                .where(CleanupTask.unit_id == unit_id)
                .order_by(CleanupTask.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if existing is not None:
            return {"deletion_id": int(existing.id), "cleanup_status": existing.status}
        # 墓碑在而任务缺失（历史边界数据）：补建任务，走同样的收敛路径。
        task = CleanupTask(unit_id=unit_id, deletion_id=uuid4())
        session.add(task)
        await session.flush()
        return {"deletion_id": int(task.id), "cleanup_status": task.status}

    if int(unit.revision) != int(expected_revision):
        raise BizError("REVISION_CONFLICT")

    new_revision = await _revision_guard(
        session, unit_id=unit_id, expected_revision=expected_revision, is_deleted=True
    )
    _ = new_revision
    task = CleanupTask(unit_id=unit_id, deletion_id=uuid4())
    session.add(task)
    await session.flush()

    # 派生内容失效（FAQ 来源转 stale 等）属 F-06.07/M06，组件未建——无动作可做；
    # H18 物理清理由清理执行器执行（未建），任务停留在 queued 是当前诚实状态。
    _log(
        session, ctx.user_id, "knowledge.delete_unit", "knowledge_unit", unit_id,
        {"is_deleted": False, "revision": int(unit.revision)},
        {"is_deleted": True, "cleanup_task_id": int(task.id)},
    )
    return {"deletion_id": int(task.id), "cleanup_status": task.status}


# ---------------------------------------------------------------- F-04.08 / API-S04


async def get_acl(
    session: AsyncSession, ctx: UserCtx, *, unit_id: int
) -> dict[str, Any]:
    """API-S04：权限弹窗回填（kb:perm）。只回填配置，不扩正文读权。"""
    unit = await _require_unit(session, unit_id)
    depts, roles, users = await _acl_maps(session, [unit_id])
    return {
        "global": bool(unit.is_global),
        "depts": depts.get(unit_id, []),
        "roles": roles.get(unit_id, []),
        "users": users.get(unit_id, []),
        "revision": int(unit.revision),
    }


async def update_acl(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    unit_id: int,
    is_global: bool,
    depts: list[int],
    roles: list[int],
    users: list[int],
    expected_revision: int,
) -> dict[str, Any]:
    """F-04.08：四维授权配置（kb:perm）。条件替换 + acl_version+1 + before/after 审计。

    - 实体校验：部门/角色必须存在；用户必须存在**且启用**（停用账号不能被授权）。
    - `kb:perm` 可以显式授权给自己（AC-04.08：无旁路但可显式配置）——审计里
      actor 与 user 维度同在，正是这条规则的证据。
    """
    unit = await _require_unit(session, unit_id)
    dept_ids = await _require_entities_exist(
        session, Department, _validate_id_list(depts, "depts"), "部门"
    )
    role_ids = await _require_entities_exist(
        session, Role, _validate_id_list(roles, "roles"), "角色"
    )
    user_ids = await _require_entities_exist(
        session, User, _validate_id_list(users, "users"), "用户", require_enabled=True
    )

    before = await get_acl(session, ctx, unit_id=unit_id)
    # ★ 快照必须在 _revision_guard **之前**取：Core UPDATE 的 evaluate 同步策略会把
    #   `acl_version = acl_version + 1` 同时应用进内存里的 ORM 实例——若在更新后读
    #   `unit.acl_version` 再 +1，等于把同一自增算了两次（响应 3、库里 2，实测复现）。
    old_acl_version = int(unit.acl_version)
    before_revision = int(unit.revision)
    new_revision = await _revision_guard(
        session,
        unit_id=unit_id,
        expected_revision=expected_revision,
        is_global=bool(is_global),
        acl_version=KnowledgeUnit.acl_version + 1,
    )
    for model in _ACL_MODELS:
        await session.execute(delete(model).where(model.unit_id == unit_id))
    for model, ids in (
        (KnowledgeAclDepartment, dept_ids),
        (KnowledgeAclRole, role_ids),
        (KnowledgeAclUser, user_ids),
    ):
        for value in ids:
            session.add(model(unit_id=unit_id, subject_id=value))
    _ = before_revision
    _log(
        session, ctx.user_id, "knowledge.update_acl", "knowledge_unit", unit_id,
        {
            "global": before["global"], "depts": before["depts"],
            "roles": before["roles"], "users": before["users"],
            "acl_version": old_acl_version,
        },
        {
            "global": bool(is_global), "depts": dept_ids, "roles": role_ids,
            "users": user_ids, "acl_version": old_acl_version + 1,
        },
    )
    return {"acl_version": old_acl_version + 1, "revision": new_revision}


# ---------------------------------------------------------------- F-04.09


async def list_acl_entities(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    kind: str,
    q: str = "",
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """F-04.09：权限弹窗的实体选择（kb:perm）。最小字段，不返回非必要资料。"""
    _check_paging(page, size)
    q = _norm_q(q)
    if kind not in ("dept", "role", "user"):
        raise BizError("INVALID_ARGUMENT", f"kind 必须为 dept/role/user，当前 {kind!r}")

    if kind == "dept":
        conditions = _like_conditions(Department.name, q)
        model, order_col = Department, Department.name
        make = lambda row: {"id": int(row.id), "label": row.name, "parent_id": row.parent_id}  # noqa: E731
    elif kind == "role":
        conditions = _like_conditions(Role.name, q)
        model, order_col = Role, Role.name
        make = lambda row: {"id": int(row.id), "label": row.name, "parent_id": None}  # noqa: E731
    else:
        conditions = [User.enabled.is_(True), *_like_conditions(User.username, q)]
        model, order_col = User, User.username
        make = lambda row: {"id": int(row.id), "label": row.username, "parent_id": row.dept_id}  # noqa: E731

    total = int(
        (
            await session.execute(select(func.count()).select_from(model).where(*conditions))
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(model)
                .where(*conditions)
                .order_by(order_col.asc(), model.id.asc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [make(row) for row in rows], "total": total}


def _like_conditions(column: Any, q: str) -> tuple[Any, ...]:
    if q:
        return (column.like(f"%{q}%"),)
    return ()


__all__ = [
    "delete_unit",
    "get_acl",
    "list_acl_entities",
    "list_units",
    "mutate_chunks",
    "read_chunks",
    "replace_document",
    "set_enabled",
    "update_acl",
    "update_metadata",
]
