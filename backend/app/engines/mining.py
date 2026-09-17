"""FAQ 挖掘管线原语（H22—H25，FUNCTION-MAP §4）。

五步流水线（faq_svc.run_mining 串接，事务划分见各函数）：
领取（H22）→ 向量化（H11）→ 聚类（H23，纯函数）→ 起草（H24，模型调用
**不占长事务**）→ 提交（H25，事务写候选/归簇/消费）。

三条硬规则：
1. **领取不只取最大 ID**（MiningConsumption 注释）：按"未消费"条件扫描，
   晚提交的日志才能被后续扫描领到。
2. **聚类禁止 A-B-C 相似链**：成员只与**代表**比较相似度——链式合并会把
   "假期/薪酬/考勤"三个主题粘成一个巨簇。
3. **部门×来源边界**：不同来源单元集合（或不同部门）的问题不进同一簇——
   FAQ 的可读性由来源推导，混来源聚类会造成"授权越聚越宽"。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.cancel import CancelSignal
from app.core.errors import TaskError
from app.models import FaqDraftJob, MiningConsumption, QaAudit
from app.db.base import utcnow


@dataclass(frozen=True, slots=True)
class QuestionVector:
    """FUNCTION-MAP §1：log_id、question、vector、dept_id、sources。"""

    log_id: int
    question: str
    vector: list[float]
    dept_id: int | None
    sources: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class QuestionCluster:
    """FUNCTION-MAP §1：cluster_id、representative、members、frequency、sources、score。"""

    cluster_id: int | None
    representative: str
    members: list[QuestionVector]
    frequency: int
    sources: list[dict[str, Any]]
    score: float


@dataclass(frozen=True, slots=True)
class MiningBatch:
    """FUNCTION-MAP §1：run_id、pipeline_version、lease_token、log_ids、questions。"""

    run_id: int
    pipeline_version: str
    lease_token: str
    log_ids: list[int]
    questions: list[QuestionVector]


@dataclass(frozen=True, slots=True)
class FaqDraft:
    """FUNCTION-MAP §1：job_key、question、answer、sources、cluster_id、confidence、model_version。"""

    job_key: str
    question: str
    answer: str
    sources: list[dict[str, Any]]
    cluster_id: int | None
    confidence: float
    model_version: str


# ---------------------------------------------------------------- H22


async def claim_logs(
    session: AsyncSession, run_id: int, pipeline_version: str, limit: int
) -> MiningBatch:
    """H22：领取终态未消费的问答日志（面向已回答问题——FAQ 从真实问答里来）。"""
    lease_token = hashlib.sha256(f"{run_id}:{utcnow()}".encode()).hexdigest()[:32]
    claimed: list[tuple[int, str]] = []
    rows = (
        (
            await session.execute(
                select(QaAudit)
                .where(QaAudit.status == "completed")
                .order_by(QaAudit.request_id.asc())
                .limit(limit * 3)  # 领取冗余：部分可能已被其他 run 消费
            )
        )
        .scalars()
        .all()
    )
    for audit in rows:
        if len(claimed) >= limit:
            break
        log_id = int(audit.request_id)
        exists = (
            await session.execute(
                select(MiningConsumption.log_id).where(
                    MiningConsumption.log_id == log_id,
                    MiningConsumption.pipeline_version == pipeline_version,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            MiningConsumption(
                log_id=log_id,
                pipeline_version=pipeline_version,
                run_id=run_id,
                status="claimed",
                lease_token=lease_token,
            )
        )
        claimed.append((log_id, audit.question))
    return MiningBatch(
        run_id=run_id,
        pipeline_version=pipeline_version,
        lease_token=lease_token,
        log_ids=[item[0] for item in claimed],
        questions=[],  # 由 run_mining 在 H11 向量化后填充
    )


# ---------------------------------------------------------------- H23（纯）


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _source_boundary(question: QuestionVector) -> tuple:
    """部门 × 来源单元集合：任一不同即不可同簇（可读性边界）。"""
    return (
        question.dept_id or 0,
        tuple(sorted({int(s["unit_id"]) for s in question.sources})),
    )


def cluster_questions(
    questions: list[QuestionVector], threshold: float
) -> list[QuestionCluster]:
    """H23：代表中心聚类——成员只与**代表**比较，禁止 A-B-C 链式合并。"""
    if not 0 < threshold <= 1:
        raise TaskError("INVALID_ARGUMENT", f"threshold 必须在 (0,1]，当前 {threshold}", transient=False)
    groups: dict[tuple, list[QuestionVector]] = {}
    for question in questions:
        groups.setdefault(_source_boundary(question), []).append(question)

    clusters: list[QuestionCluster] = []
    for group in groups.values():
        remaining = list(group)
        while remaining:
            representative = remaining.pop(0)
            members = [representative]
            for candidate in list(remaining):
                if _cosine(representative.vector, candidate.vector) >= threshold:
                    members.append(candidate)
                    remaining.remove(candidate)
            all_sources: dict[tuple[int, int], dict[str, Any]] = {}
            for member in members:
                for source in member.sources:
                    all_sources[(int(source["unit_id"]), int(source["version"]))] = source
            score = 1.0 if len(members) == 1 else max(
                _cosine(representative.vector, m.vector) for m in members[1:]
            )
            clusters.append(
                QuestionCluster(
                    cluster_id=None,
                    representative=representative.question,
                    members=members,
                    frequency=len(members),
                    sources=list(all_sources.values()),
                    score=score,
                )
            )
    return clusters


# ---------------------------------------------------------------- H24 / H25


def _job_key(pipeline_version: str, representative: str, boundary: tuple) -> str:
    material = f"{pipeline_version}\x00{representative}\x00{boundary}"
    return "draft:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


async def draft_candidate(
    session: AsyncSession,
    *,
    run_id: int,
    cluster: QuestionCluster,
    cluster_id: int,
    pipeline_version: str,
    model_version: str,
    stream_llm: Any,
) -> FaqDraft:
    """H24：起草。job_key 稳定复用既有成功结果；job 行先落 pending（不占长事务），
    模型调用在事务外，答案由 H25 提交时回填。调用方须已开启事务（用于 pending 行）。"""
    boundary = _source_boundary(cluster.members[0])
    job_key = _job_key(pipeline_version, cluster.representative, boundary)
    payload_hash = hashlib.sha256(
        "\x00".join(member.question for member in cluster.members).encode("utf-8")
    ).hexdigest()

    existing = (
        await session.execute(select(FaqDraftJob).where(FaqDraftJob.job_key == job_key))
    ).scalar_one_or_none()
    if existing is not None and existing.status == "done" and existing.answer:
        return FaqDraft(
            job_key=job_key,
            question=cluster.representative,
            answer=existing.answer,
            sources=cluster.sources,
            cluster_id=cluster_id,
            confidence=0.8,
            model_version=existing.model_version,
        )

    session.add(
        FaqDraftJob(
            job_key=job_key,
            run_id=run_id,
            cluster_id=cluster_id,
            payload_hash=payload_hash,
            answer=None,
            sources_snapshot={"sources": cluster.sources},
            status="pending",
            model_version=model_version,
        )
    )
    await session.flush()

    settings = get_settings()
    evidence = "\n".join(
        f"[{index}] 单元{source['unit_id']}@v{source['version']}"
        for index, source in enumerate(cluster.sources, start=1)
    )
    prompt = (
        "以下是同一主题的员工提问：\n"
        + "\n".join(f"- {member.question}" for member in cluster.members)
        + f"\n\n参考来源：\n{evidence}\n\n"
        "请写一条可直接放入 FAQ 的标准答案（200 字以内）。"
    )
    parts: list[str] = []
    async for delta in stream_llm(
        [{"role": "user", "content": prompt}], settings, CancelSignal(lambda: False)
    ):
        if delta.text:
            parts.append(delta.text)
    answer = "".join(parts).strip()
    if not answer:
        raise TaskError("DRAFT_EMPTY", "起草结果为空", transient=True)
    return FaqDraft(
        job_key=job_key,
        question=cluster.representative,
        answer=answer,
        sources=cluster.sources,
        cluster_id=cluster_id,
        confidence=0.8,
        model_version=model_version,
    )


async def mark_drafts_done(session: AsyncSession, drafts: list[FaqDraft]) -> None:
    """H25 的一部分：起草结果回填（与候选/消费同一事务提交）。"""
    for draft in drafts:
        await session.execute(
            update(FaqDraftJob)
            .where(FaqDraftJob.job_key == draft.job_key)
            .values(answer=draft.answer, status="done")
        )
