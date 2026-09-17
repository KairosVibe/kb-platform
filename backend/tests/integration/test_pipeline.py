"""F-03.04 索引流水线集成测试（真实 MySQL；向量化与向量库用测试替身）。

替身注入点（用户决策"B 先行、A 补验"）：
- `app.providers.embedding.embed_batches` → 确定性假向量；
- `app.providers.vector_store` 的写入/计数 → 内存计数。

真实链路（DashScope + Milvus）由 E2E 脚本补验，不进自动化套件——它依赖外网与云凭据，
进了 CI 会把"云服务抖动"误报成"代码回归"。

五条路径各自钉住一个会悄悄失守的状态机规则（PRD §1.2 第 2 条）：
- 成功：`indexed_version` 推进且任务 `succeeded`；
- 版本过时：`superseded`（不是 failed——旧任务被取代属正常收敛）；
- 瞬时失败：`retry_wait` + `next_retry_at`（attempts 保留）；
- 永久失败：`failed`（不循环）；
- 无租约：**什么都不写**（H15"失败停止写入和提交"）。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.errors import TaskError
from app.db.base import utcnow
from app.providers import embedding as embedding_module
from app.providers import vector_store as vector_store_module
from app.services import ingest_svc
from app.tasks.lease import claim_task
from tests.integration.conftest import it_dsn, run

BODY = "# 薪酬制度\n\n" + "".join(
    f"第{i}条 员工薪酬按月发放，逢节假日提前。" for i in range(1, 12)
)


def _fake_vector(text: str, dim: int) -> list[float]:
    """确定性测试向量：由文本哈希派生并归一化（余弦检索能命中自身）。

    ★ 只活在测试里（"B 先行"的替身）。它**不具备语义**——不同文本间的相似度
      没有意义，唯一保证是"同文本向量相同、可被最近邻召回"。曾误放在
      `app/providers/embedding.py`，被符号门禁正确拦下（生产代码不应有测试专用符号）。
    """
    import hashlib
    import math

    seed = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    while len(values) < dim:
        seed = hashlib.sha256(seed).digest()
        values.extend(byte for byte in seed[:32])
        values = values[:dim] if len(values) >= dim else values
        if len(values) < dim:
            continue
    raw = [value / 255.0 - 0.5 for value in values[:dim]]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


async def _seed_task(content: bytes = BODY.encode("utf-8"), *, target_version: int = 1) -> dict:
    """建 1 账号 + 1 单元 + 1 版本记录 + 1 任务，并把文件写入受控存储。"""
    settings = get_settings()
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                user_id = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO `user` (username, username_norm, password_hash, dept_id, "
                                "enabled, identity_revision, revision, created_at, updated_at) "
                                "VALUES (:u, :n, 'x', NULL, 1, 1, 1, NOW(6), NOW(6))"
                            ),
                            {"u": f"uploader{uuid4().hex[:6]}", "n": uuid4().hex[:8]},
                        )
                    ).lastrowid
                )
                unit_id = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO knowledge_unit (code, title, format, category, creator_id, "
                                "enabled, is_deleted, is_global, content_version, indexed_version, "
                                "index_status, acl_version, revision, created_at, updated_at) "
                                "VALUES (:c, '流水线用例', 'md', '制度', :u, 1, 0, 0, :cv, NULL, "
                                "'pending', 1, 1, NOW(6), NOW(6))"
                            ),
                            {"c": f"KB-{uuid4().hex[:8]}", "u": user_id, "cv": target_version},
                        )
                    ).lastrowid
                )
                version_id = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO knowledge_version (unit_id, version, file_key, sha256, "
                                "parser_version, chunk_config, embedding_model_version, index_generation, "
                                "created_at, updated_at) "
                                "VALUES (:u, :v, :fk, :sha, :pv, :cc, 'fake:t:1024', 'fake:t:1024', "
                                "NOW(6), NOW(6))"
                            ),
                            {
                                "u": unit_id,
                                "v": target_version,
                                "fk": f"{uuid4().hex}.md",
                                "sha": "0" * 64,
                                "pv": "h19.r1",
                                "cc": '{"size": 60, "overlap": 15}',
                            },
                        )
                    ).lastrowid
                )
                task_id = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO index_task (unit_id, target_version, status, attempts, "
                                "revision, created_at, updated_at) "
                                "VALUES (:u, :v, 'queued', 0, 1, NOW(6), NOW(6))"
                            ),
                            {"u": unit_id, "v": target_version},
                        )
                    ).lastrowid
                )
        stored = Path(settings.upload_dir) / f"{version_id:x}.md"
        stored.parent.mkdir(parents=True, exist_ok=True)
        # file_key 写进版本记录：测试直接按记录的 file_key 落盘（模拟受理阶段的产物）
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE knowledge_version SET file_key=:fk WHERE id=:i"),
                    {"fk": stored.name, "i": version_id},
                )
        stored.write_bytes(content)
        return {"unit_id": unit_id, "task_id": task_id, "file": stored, "user_id": user_id}
    finally:
        await engine.dispose()


def _install_fakes(monkeypatch: pytest.MonkeyPatch, *, fail_embed: TaskError | None = None) -> dict:
    """注入测试替身，返回调用计数器（供断言"确实走了替身"）。"""
    settings = get_settings()
    calls = {"embed": 0, "write": 0, "count": 0}
    dim = int(settings.embed_dim)

    async def fake_embed(texts, config):
        calls["embed"] += 1
        if fail_embed is not None:
            raise fail_embed
        return [_fake_vector(text, dim) for text in texts], {
            "prompt_tokens": sum(len(t) for t in texts),
            "total_tokens": sum(len(t) for t in texts),
        }, "fake:t:1024"

    async def fake_write(config, *, rows):
        calls["write"] += 1
        return len(rows)

    def fake_count(config, *, unit_id, version, expected=None):
        calls["count"] += 1
        counted = _counted.get((unit_id, version), 0)
        # 真实现带 expected 轮询（最终一致性）；替身直接返回记账值，
        # 但如果期望与记账不一致，说明测试自身的替身装错了——立即暴露。
        if expected is not None and counted != expected:
            raise AssertionError(f"替身计数 {counted} != 期望 {expected}")
        return counted

    counted: dict[tuple[int, int], int] = {}
    _counted = counted

    def write_and_count(config, *, rows):
        counted[(rows[0]["unit_id"], rows[0]["version"])] = len(rows)
        return fake_write(config, rows=rows)

    monkeypatch.setattr(embedding_module, "embed_batches", fake_embed)
    monkeypatch.setattr(vector_store_module, "upsert_version_chunks", write_and_count)
    monkeypatch.setattr(vector_store_module, "count_version_chunks", fake_count)
    return calls


def test_pipeline_succeeds_and_activates(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """成功路径：解析→切片→向量化→写入→激活，MySQL 与任务状态全部推进。"""
    calls = _install_fakes(monkeypatch)

    async def _run() -> None:
        seeded = await _seed_task()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                lease = await claim_task(
                    session, task_id=seeded["task_id"], worker_id="test", now=utcnow()
                )
                assert lease is not None
                result = await ingest_svc.run_pipeline(
                    session, task_id=seeded["task_id"], lease_token=lease.lease_token
                )
            assert result["status"] == "succeeded", result
            assert result["indexed_version"] == 1 and result["error_code"] is None
            assert calls["embed"] >= 1 and calls["count"] == 1

            async with engine.connect() as conn:
                unit = (
                    await conn.execute(
                        text(
                            "SELECT indexed_version, index_status FROM knowledge_unit WHERE id=:u"
                        ),
                        {"u": seeded["unit_id"]},
                    )
                ).first()
                assert (int(unit.indexed_version), unit.index_status) == (1, "indexed")
                task = (
                    await conn.execute(
                        text("SELECT status, error_code, stage FROM index_task WHERE id=:t"),
                        {"t": seeded["task_id"]},
                    )
                ).first()
                assert (task.status, task.error_code, task.stage) == ("succeeded", None, None)
                chunks = await conn.scalar(
                    text("SELECT COUNT(*) FROM chunk WHERE unit_id=:u"),
                    {"u": seeded["unit_id"]},
                )
                assert int(chunks) >= 1
                stable = await conn.scalar(
                    text("SELECT MIN(id) FROM chunk WHERE unit_id=:u"),
                    {"u": seeded["unit_id"]},
                )
                assert int(stable) > 0
        finally:
            await engine.dispose()
            seeded["file"].unlink(missing_ok=True)

    run(_run())


def test_pipeline_marks_stale_version_superseded(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧任务被新版本取代 → superseded（不是 failed，不污染失败率）。"""
    _install_fakes(monkeypatch)

    async def _run() -> None:
        seeded = await _seed_task(target_version=2)  # content_version=2 但任务 target=1
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        text("UPDATE index_task SET target_version=1 WHERE id=:t"),
                        {"t": seeded["task_id"]},
                    )
                lease = await claim_task(
                    session, task_id=seeded["task_id"], worker_id="test", now=utcnow()
                )
                result = await ingest_svc.run_pipeline(
                    session, task_id=seeded["task_id"], lease_token=lease.lease_token
                )
            assert result["status"] == "superseded"
            assert result["error_code"] == "SUPERSEDED"
            async with engine.connect() as conn:
                status = await conn.scalar(
                    text("SELECT status FROM index_task WHERE id=:t"),
                    {"t": seeded["task_id"]},
                )
                assert status == "superseded"
        finally:
            await engine.dispose()
            seeded["file"].unlink(missing_ok=True)

    run(_run())


def test_pipeline_transient_failure_waits_for_retry(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """瞬时失败（限流）→ retry_wait + next_retry_at；attempts 保留。"""
    _install_fakes(
        monkeypatch,
        fail_embed=TaskError("EMBED_RATE_LIMITED", "限流", transient=True),
    )

    async def _run() -> None:
        seeded = await _seed_task()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                lease = await claim_task(
                    session, task_id=seeded["task_id"], worker_id="test", now=utcnow()
                )
                result = await ingest_svc.run_pipeline(
                    session, task_id=seeded["task_id"], lease_token=lease.lease_token
                )
            assert result["status"] == "retry_wait"
            assert result["error_code"] == "EMBED_RATE_LIMITED"
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT status, attempts, next_retry_at, lease_token FROM index_task "
                            "WHERE id=:t"
                        ),
                        {"t": seeded["task_id"]},
                    )
                ).first()
                assert row.status == "retry_wait"
                assert int(row.attempts) == 1, "attempts 保留历史，不清零"
                assert row.next_retry_at is not None
                assert row.lease_token is None, "重排后租约必须释放，否则 H14 无法领取"
        finally:
            await engine.dispose()
            seeded["file"].unlink(missing_ok=True)

    run(_run())


def test_pipeline_permanent_failure_stops_retrying(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """永久失败（解析空文）→ failed；**不**设置 next_retry_at（不循环）。"""
    _install_fakes(monkeypatch)

    async def _run() -> None:
        seeded = await _seed_task(content=b"")  # 空文件：清洗后必为空
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                lease = await claim_task(
                    session, task_id=seeded["task_id"], worker_id="test", now=utcnow()
                )
                result = await ingest_svc.run_pipeline(
                    session, task_id=seeded["task_id"], lease_token=lease.lease_token
                )
            assert result["status"] == "failed"
            assert result["error_code"] in ("PARSE_EMPTY", "PARSE_CORRUPT")
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT status, next_retry_at FROM index_task WHERE id=:t"),
                        {"t": seeded["task_id"]},
                    )
                ).first()
                assert row.status == "failed"
                assert row.next_retry_at is None, "永久错误不得再排期"
        finally:
            await engine.dispose()
            seeded["file"].unlink(missing_ok=True)

    run(_run())


def test_pipeline_without_lease_writes_nothing(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H15"失败停止写入和提交"：租约不匹配时**不得**改任何状态。"""
    _install_fakes(monkeypatch)

    async def _run() -> None:
        seeded = await _seed_task()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                with pytest.raises(TaskError) as exc:
                    await ingest_svc.run_pipeline(
                        session, task_id=seeded["task_id"], lease_token=uuid4()
                    )
            assert exc.value.code == "LEASE_INVALID"
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT status, stage, attempts FROM index_task WHERE id=:t"),
                        {"t": seeded["task_id"]},
                    )
                ).first()
                assert (row.status, row.stage, int(row.attempts)) == ("queued", None, 0)
        finally:
            await engine.dispose()
            seeded["file"].unlink(missing_ok=True)

    run(_run())
