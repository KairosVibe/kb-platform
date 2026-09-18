"""极简常驻 worker（演示/联调环境）：领取索引任务执行流水线 + 执行清理任务。

★ 这补的是"常驻调度进程"缺口（F-03.06/H18 的执行侧）：
  上传受理后 index_task 停在 queued，必须有一个进程领取并执行 run_pipeline；
  删除后的 cleanup_task 同理（run_cleanup_due）。
  生产部署用多 worker + 进程管理器；本脚本是单进程最小实现。
"""

import asyncio
import sys

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.models import IndexTask  # noqa: E402
from app.services.ingest_svc import run_pipeline  # noqa: E402
from app.tasks.cleanup import run_cleanup_due  # noqa: E402
from app.tasks.lease import claim_task  # noqa: E402


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    print(f"[mini-worker] 轮询中（db={url.database if (url := None) else 'configured'}）", flush=True)
    while True:
        try:
            async with factory() as session:
                ids = (
                    (
                        await session.execute(
                            select(IndexTask.id)
                            .where(IndexTask.status == "queued")
                            .order_by(IndexTask.id.asc())
                            .limit(5)
                        )
                    )
                    .scalars()
                    .all()
                )
            for task_id in ids:
                async with factory() as session:
                    lease = await claim_task(
                        session, task_id=int(task_id), worker_id="mini-worker", now=utcnow()
                    )
                    if lease is None:
                        continue
                    result = await run_pipeline(
                        session, task_id=int(task_id), lease_token=lease.lease_token
                    )
                    print(f"[mini-worker] index_task {task_id} → {result['status']}", flush=True)
            async with factory() as session:
                cleaned = await run_cleanup_due(session, worker_id="mini-worker", now=utcnow())
            for r in cleaned:
                print(f"[mini-worker] cleanup → {r['status']}", flush=True)
        except Exception as exc:  # noqa: BLE001 — 常驻进程兜底
            print(f"[mini-worker] error: {exc}", flush=True)
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
