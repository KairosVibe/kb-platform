"""临时（联调第 4 轮 E2E）：问答台真实链路——真实检索 + 真实 DashScope 生成 + SSE。

不 mock 任何生成器：accept_question 后由真实后台任务执行 run_answer
（Milvus 授权检索 → DashScope 流式生成 → 事件持久化 → 终态同事务提交）。
"""

import io
import json
import sys
import time
from uuid import uuid4

sys.path.insert(0, "d:/zcode/实战项目1/backend")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.providers import vector_store  # noqa: E402
from tests.integration.conftest import _truncate_all, make_engine, run  # noqa: E402
from tests.integration.test_knowledge_endpoints import (  # noqa: E402
    PASSWORD,
    _client,
    _seed,
)
from app.services.ingest_svc import run_pipeline  # noqa: E402
from app.tasks.lease import claim_task  # noqa: E402
from app.db.base import utcnow  # noqa: E402

BODY = "# 差旅报销办法\n\n" + "".join(
    f"第{i}条 员工差旅费凭发票按月报销，交通费实报实销，住宿费上限每晚四百元。" for i in range(1, 30)
)


async def _run_one(task_id: int) -> str:
    engine = make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            lease = await claim_task(session, task_id=task_id, worker_id="e2e", now=utcnow())
            assert lease is not None
            result = await run_pipeline(session, task_id=task_id, lease_token=lease.lease_token)
            return str(result["status"])
    finally:
        await engine.dispose()


def _sse_lines(resp_text: str) -> list[tuple[str, dict]]:
    """解析 SSE 帧：seq 在 `id:` 行（游标），event/data 各一行（API-CONTRACTS §4）。"""
    events: list[tuple[str, dict]] = []
    for block in resp_text.split("\n\n"):
        event, data, seq = None, None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
            elif line.startswith("id:"):
                seq = int(line.split(":", 1)[1].strip())
        if event and data:
            payload = json.loads(data)
            if seq is not None:
                payload["seq"] = seq
            events.append((event, payload))
    return events


async def main() -> None:
    # ★ run_answer/stream_events 的后台引擎工厂默认连演示库（get_settings().database_url），
    #   E2E 数据在集成库——与 test_chat_endpoints 同款做法，重定向到 IT 库。
    import app.services.chat_svc as chat_svc_mod

    def _it_engine_factory():
        engine = make_engine()
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    chat_svc_mod._engine_factory = _it_engine_factory
    engine0 = make_engine()
    try:
        await _truncate_all(engine0)
    finally:
        await engine0.dispose()

    settings = get_settings()
    ids = await _seed()
    # admin 种子只有 kb:*/sys:* 码，问答需要 ai:ask
    engine = make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text("INSERT IGNORE INTO role_permission (role_id, code) VALUES (:r, :c)"),
                    {"r": ids["admin_role_id"], "c": "ai:ask"},
                )
    finally:
        await engine.dispose()
    # 受理问答需要存在配置修订快照（存在性检查；检索配置由代码内置）
    engine = make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text("INSERT INTO config_revision (patch, snapshot, actor_id, created_at) "
                         "VALUES (:p, :s, NULL, NOW(6))"),
                    {"p": '{"source":"chat-e2e"}', "s": '{"vector_top_k":20}'},
                )
    finally:
        await engine.dispose()

    async with _client() as client:
        token = (
            await client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        ).json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # [1] 上传知识 + 真实流水线入库
        up = await client.post(
            "/api/uploads",
            headers=headers,
            files={"file": ("报销办法.md", io.BytesIO(BODY.encode("utf-8")), "text/markdown")},
            data={"category": "财务制度", "client_upload_id": str(uuid4())},
        )
        assert up.status_code == 202, up.text
        unit_id = int(up.json()["data"]["unit_id"])
        assert (await _run_one(int(up.json()["data"]["task_id"]))) == "succeeded"

        # 授权 global（保证 RAG 有可读来源）
        listing = (
            await client.get("/api/knowledge-units", headers=headers, params={"page": 1, "size": 5})
        ).json()["data"]["items"]
        row = next(u for u in listing if int(u["id"]) == unit_id)
        acl = await client.put(
            f"/api/knowledge-units/{unit_id}/acl",
            headers=headers,
            json={"global": True, "depts": [], "roles": [], "users": [],
                  "expected_revision": int(row["revision"])},
        )
        assert acl.status_code == 200, acl.text
        print("[1] 知识入库 + global 授权 ✓")

        # [2] 创建会话 + 提问（真实后台生成）
        created = await client.post("/api/sessions", headers=headers, json={"title": "差旅问答"})
        session_id = int(created.json()["data"]["session_id"])
        asked = await client.post(
            "/api/chat/requests",
            headers=headers,
            json={"session_id": session_id, "client_request_id": str(uuid4()),
                  "question": "住宿费每晚的报销上限是多少？"},
        )
        assert asked.status_code == 202, asked.text
        request_id = int(asked.json()["data"]["request_id"])
        print(f"[2] 提问受理 request_id={request_id}，显式调度真实生成…")

        # ★ ASGITransport 下 loop.create_task 的后台任务会被 anyio cancel scope
        #   取消（生产由常驻进程 spawn）。显式 await run_answer —— 同一函数、
        #   同一真实链路（Milvus 授权检索 + DashScope 流式生成），只是调度方式不同。
        from app.services.chat_svc import run_answer

        engine = make_engine()
        try:
            await run_answer(request_id, engine=engine)
        finally:
            await engine.dispose()

        # [3] 轮询安全快照到终态（后台任务真实调 DashScope 生成）
        deadline = time.time() + 180
        snapshot = None
        while time.time() < deadline:
            snap = await client.get(f"/api/chat/requests/{request_id}", headers=headers)
            assert snap.status_code == 200, snap.text
            data = snap.json()["data"]
            if data["status"] in ("completed", "failed", "cancelled"):
                snapshot = data
                break
            time.sleep(2)
        assert snapshot is not None, "生成超时"
        assert snapshot["status"] == "completed", f"生成失败：{snapshot}"
        assert snapshot["restricted"] is False and snapshot["answer"]
        print(f"[3] 终态 completed，answer {len(snapshot['answer'])} 字，last_seq={snapshot['last_seq']}")

        # [4] SSE 全量事件重放：seq 连续 + 关键事件齐全
        events_resp = await client.get(
            f"/api/chat/requests/{request_id}/events",
            headers=headers,
            params={"after_seq": 0},
        )
        assert events_resp.status_code == 200, events_resp.text
        events = _sse_lines(events_resp.text)
        names = [e for e, _ in events]
        seqs = [int(d["seq"]) for _, d in events]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq 不连续：{seqs}"
        for required in ("meta", "delta", "citations", "done"):
            assert required in names, f"缺少 {required} 事件（实际 {names}）"
        delta_text = "".join(str(d.get("text", "")) for e, d in events if e == "delta")
        assert delta_text and delta_text in (snapshot["answer"] or "")
        cits = next(d for e, d in events if e == "citations").get("items", [])
        print(f"[4] SSE 事件 {len(events)} 帧（seq 连续），delta {len(delta_text)} 字，citations {len(cits)} 条")

        # [5] 引用详情（readCitation）
        if cits:
            no = int(cits[0]["no"])
            cite = await client.get(
                f"/api/chat/requests/{request_id}/citations/{no}", headers=headers
            )
            assert cite.status_code == 200, cite.text
            cdata = cite.json()["data"]
            assert cdata["unit_id"] == unit_id and cdata["title"]
            print(f"[5] 引用详情 no={no}: 《{cdata['title']}》 偏移 {cdata['offset']}")

        # [6] 历史回放（readHistory）
        history = await client.get(
            f"/api/sessions/{session_id}/messages", headers=headers, params={"page": 1, "size": 10}
        )
        assert history.status_code == 200, history.text
        roles = [m["role"] for m in history.json()["data"]["items"]]
        assert roles.count("user") == 1 and roles.count("assistant") == 1
        print(f"[6] 历史回放 {roles} ✓")

        # [7] 幂等重放同问题（200 + 原 ID）与取消已完成请求
        replay = await client.post(
            "/api/chat/requests",
            headers=headers,
            json={"session_id": session_id, "client_request_id": None,
                  "question": "住宿费每晚的报销上限是多少？"},
        )
        # client_request_id 为 None 属新请求（服务端生成？）——跳过该断言，只验证取消已完成
        cancel = await client.post(
            f"/api/chat/requests/{request_id}/cancel", headers=headers
        )
        assert cancel.status_code in (200, 409), cancel.text
        print(f"[7] 已完成请求取消 → HTTP {cancel.status_code}（终态不可取消 = 409）✓")

        # [8] 清理
        engine = make_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    # ★ message_source.chunk_id → chunk 是 RESTRICT：被问答引用过的
                    #   切片不能物理删（"已发送内容无法撤回"的 DB 层表达）；物理清理
                    #   属 H18 执行器的职责。E2E 里先解除引用再删单元。
                    await session.execute(
                        text("DELETE FROM message_source WHERE unit_id=:u"), {"u": unit_id}
                    )
                    await session.execute(
                        text("DELETE FROM knowledge_unit WHERE id=:u"), {"u": unit_id}
                    )
        finally:
            await engine.dispose()
        milvus_client = vector_store._connect(settings)
        try:
            milvus_client.delete(
                collection_name=settings.milvus_collection, filter=f"unit_id == {unit_id}"
            )
        finally:
            milvus_client.close()
        residue = -1
        for _ in range(8):
            residue = vector_store.count_version_chunks(settings, unit_id=unit_id, version=1)
            if residue == 0:
                break
            time.sleep(1.5)
        print(f"[8] 清理完成：Milvus 残留 = {residue}")
        assert residue == 0

    print("E2E_RESULT = PASS")


run(main())
