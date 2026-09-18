"""H18 清理执行器集成测试（真实 MySQL + 真实 Milvus）。

覆盖：成功路径（向量/文件清理 + 墓碑行保留）、墓碑缺失、瞬时失败重试、
共享文件引用未归零跳过、幂等（无任务领取返回空）。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.providers import vector_store as vector_store_module
from app.services.auth_svc import _reset_login_limiter
from app.services.ingest_svc import run_pipeline
from app.tasks import cleanup as cleanup_mod
from app.tasks.cleanup import claim_cleanup_task, cleanup_deleted, run_cleanup_due
from app.tasks.lease import claim_task
from app.db.base import utcnow
from tests.integration.conftest import _truncate_all, it_dsn, make_engine, run
from tests.integration.test_knowledge_endpoints import (
    LONG_BODY,
    PASSWORD,
    _auth,
    _client,
    _seed,
    _upload,
)


async def _run_index(task_id: int) -> str:
    engine = make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            lease = await claim_task(session, task_id=task_id, worker_id="t", now=utcnow())
            assert lease is not None
            result = await run_pipeline(session, task_id=task_id, lease_token=lease.lease_token)
            return str(result["status"])
    finally:
        await engine.dispose()


async def _sql_all(statement: str) -> list[tuple]:
    engine = make_engine()
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(statement))).all())
    finally:
        await engine.dispose()


async def _sql_exec(statement: str) -> None:
    engine = make_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


def test_cleanup_success_and_artifacts_gone(clean_db: None) -> None:
    """成功路径：向量归零、文件删除、unit/version/chunk 行保留墓碑态、task=done。"""

    async def _run() -> None:
        await _seed()
        settings = get_settings()
        async with _client() as client:
            admin = (
                await client.post(
                    "/api/auth/login", json={"username": "admin", "password": PASSWORD}
                )
            ).json()["data"]["access_token"]

            up = await _upload(client, admin)
            assert (await _run_index(int(up["task_id"]))) == "succeeded"
            unit_id = int(up["unit_id"])

            file_keys = [
                str(r[0])
                for r in await _sql_all(
                    f"SELECT file_key FROM knowledge_version WHERE unit_id={unit_id}"
                )
            ]
            assert file_keys
            stored = [Path(settings.upload_dir) / k for k in file_keys]
            assert all(p.is_file() for p in stored), "索引完成前受控文件必须存在"
            before_vectors = vector_store_module.count_unit_vectors(
                settings, unit_id=unit_id
            )
            assert before_vectors > 0

            # 删除（打墓碑 + 清理任务）
            listing = (
                await client.get(
                    "/api/knowledge-units", headers=_auth(admin), params={"page": 1, "size": 5}
                )
            ).json()["data"]["items"]
            revision = next(int(u["revision"]) for u in listing if int(u["id"]) == unit_id)
            deleted = await client.request(
                "DELETE",
                f"/api/knowledge-units/{unit_id}",
                headers=_auth(admin),
                json={"expected_revision": revision},
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["data"]["cleanup_status"] == "queued"

            # 执行清理
            results = await _schedule()
            assert len(results) == 1 and results[0]["status"] == "done"
            # removed = 向量条数 + 删除的文件数（本用例单版本单文件）
            assert int(results[0]["removed"]) == before_vectors + 1

            # 核对：向量归零、文件删除、行保留墓碑态
            assert vector_store_module.count_unit_vectors(settings, unit_id=unit_id) == 0
            assert not any(p.exists() for p in stored)
            unit_state = await _sql_all(
                f"SELECT is_deleted FROM knowledge_unit WHERE id={unit_id}"
            )
            assert unit_state == [(True,)], "unit 行保留墓碑态（不复活也不物理消失）"
            chunk_rows = await _sql_all(f"SELECT COUNT(*) FROM chunk WHERE unit_id={unit_id}")
            assert chunk_rows == [(before_vectors,)], "切片行保留（引用元数据可解析）"
            task_state = await _sql_all(
                f"SELECT status FROM cleanup_task WHERE unit_id={unit_id}"
            )
            assert task_state == [("done",)]

            # 幂等：无剩余任务
            assert await _schedule() == []

    run(_run())


async def _schedule() -> list[dict[str, Any]]:
    engine = make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await run_cleanup_due(session, worker_id="test", now=utcnow())
    finally:
        await engine.dispose()


def test_cleanup_no_tombstone_is_failed(clean_db: None) -> None:
    """墓碑缺失（未删除的单元被误登记任务）→ failed，不误删派生物。"""

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            admin = (
                await client.post(
                    "/api/auth/login", json={"username": "admin", "password": PASSWORD}
                )
            ).json()["data"]["access_token"]
            up = await _upload(client, admin)  # 只需 unit 行存在，不必索引完成
        unit_id = int(up["unit_id"])
        await _sql_exec(
            f"INSERT INTO cleanup_task (unit_id, deletion_id, status, attempts, created_at, "
            f"updated_at) VALUES ({unit_id}, REPLACE(UUID(),'-',''), 'queued', 0, "
            f"NOW(6), NOW(6))"
        )
        settings = get_settings()
        before = vector_store_module.count_unit_vectors(settings, unit_id=unit_id)
        results = await _schedule()
        assert results[0]["status"] == "failed"
        assert results[0]["error_code"] == "CLEAN_NO_TOMBSTONE"
        assert vector_store_module.count_unit_vectors(settings, unit_id=unit_id) == before, (
            "未删除单元的向量绝不能被清理"
        )

    run(_run())


def test_cleanup_transient_failure_retries(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """向量库瞬时失败 → 回 queued；下一次调度成功收敛 done。"""

    async def _run() -> None:
        await _seed()
        settings = get_settings()
        async with _client() as client:
            admin = (
                await client.post(
                    "/api/auth/login", json={"username": "admin", "password": PASSWORD}
                )
            ).json()["data"]["access_token"]
            up = await _upload(client, admin)
            assert (await _run_index(int(up["task_id"]))) == "succeeded"
            unit_id = int(up["unit_id"])
            listing = (
                await client.get(
                    "/api/knowledge-units", headers=_auth(admin), params={"page": 1, "size": 5}
                )
            ).json()["data"]["items"]
            revision = next(int(u["revision"]) for u in listing if int(u["id"]) == unit_id)
            deleted = await client.request(
                "DELETE",
                f"/api/knowledge-units/{unit_id}",
                headers=_auth(admin),
                json={"expected_revision": revision},
            )
            assert deleted.status_code == 200

        # 第一次调度：删除失败（瞬时）
        original = vector_store_module.delete_unit_vectors

        def _boom(config: Any, *, unit_id: int) -> int:
            from app.core.errors import TaskError

            raise TaskError("VECTOR_DELETE_FAILED", "milvus down", transient=True)

        monkeypatch.setattr(vector_store_module, "delete_unit_vectors", _boom)
        results = await _schedule()
        assert results[0]["status"] == "queued", "瞬时失败应回 queued 等待重试"
        assert results[0]["error_code"] == "VECTOR_DELETE_FAILED"
        monkeypatch.undo()

        # 第二次调度：成功
        results = await _schedule()
        assert results[0]["status"] == "done", f"重试应收敛 done：{results}"
        assert vector_store_module.count_unit_vectors(settings, unit_id=unit_id) == 0

    run(_run())


def test_cleanup_shared_file_not_deleted(clean_db: None) -> None:
    """共享文件引用未归零：另一单元同 file_key 时跳过文件删除（向量仍各自清理）。"""

    async def _run() -> None:
        await _seed()
        settings = get_settings()
        async with _client() as client:
            admin = (
                await client.post(
                    "/api/auth/login", json={"username": "admin", "password": PASSWORD}
                )
            ).json()["data"]["access_token"]
            up1 = await _upload(client, admin)
            assert (await _run_index(int(up1["task_id"]))) == "succeeded"
            unit1 = int(up1["unit_id"])
            file_key = str(
                (
                    await _sql_all(f"SELECT file_key FROM knowledge_version WHERE unit_id={unit1}")
                )[0][0]
            )

            # 构造第二个单元引用同一 file_key（直接改库最直接）
            up2 = await _upload(client, admin, name="second.md")
            assert (await _run_index(int(up2["task_id"]))) == "succeeded"
            unit2 = int(up2["unit_id"])
            await _sql_exec(
                f"UPDATE knowledge_version SET file_key='{file_key}' WHERE unit_id={unit2}"
            )

            # 删除 unit1 → 清理时 file_key 仍被 unit2 引用 → 文件保留
            listing = (
                await client.get(
                    "/api/knowledge-units", headers=_auth(admin), params={"page": 1, "size": 5}
                )
            ).json()["data"]["items"]
            revision = next(int(u["revision"]) for u in listing if int(u["id"]) == unit1)
            deleted = await client.request(
                "DELETE",
                f"/api/knowledge-units/{unit1}",
                headers=_auth(admin),
                json={"expected_revision": revision},
            )
            assert deleted.status_code == 200
            results = await _schedule()
            assert results[0]["status"] == "done"
            assert (Path(settings.upload_dir) / file_key).is_file(), "共享文件必须保留"
            assert vector_store_module.count_unit_vectors(settings, unit_id=unit1) == 0
            # unit2 的向量不受影响
            assert vector_store_module.count_unit_vectors(settings, unit_id=unit2) > 0

    run(_run())
