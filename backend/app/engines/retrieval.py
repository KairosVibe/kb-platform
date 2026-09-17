"""授权检索引擎（H08 两路召回、H09 RRF 融合、H10 安全 Prompt 组装）。

与权限引擎同一条纪律：**判定与融合是纯函数，读库与向量服务集中在 IO 入口**。

三路边界（ARCHITECTURE §4.2，必须分开、不得混报）：
- `allowed`：有权且索引可用，可进入 Prompt 的证据；
- `denied_ids`：无权候选——**只进受控审计**（qa_audit.denied_snapshot），不下发客户端；
- `unavailable_ids`：有权但索引不可用（未索引/stale/版本不一致）——"索引没准备好"
  不是"无权"，也不进知识缺口。

第二路召回的现状（诚实边界）：倒排引擎尚未建设，**关键字路当前用 MySQL `LIKE`
召回**（`config_revision` 的 keyword_top_k）。能被 RRF 融合、能跑通链路，但不是
分词倒排——换引擎时只动 `_keyword_recall`，融合与授权逻辑不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import TaskError
from app.models import Chunk, KnowledgeUnit
from app.providers import embedding as embedding_provider
from app.providers import vector_store


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """带多路得分的候选切片（FUNCTION-MAP §1）。"""

    chunk_id: int
    unit_id: int
    version: int
    text: str
    dense_score: float | None = None
    keyword_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """H08 输出：三桶互斥 + 融合后证据。"""

    allowed: list[ScoredChunk] = field(default_factory=list)
    denied_ids: list[int] = field(default_factory=list)
    unavailable_ids: list[int] = field(default_factory=list)
    result_type: str = "no_evidence"
    top_score: float | None = None
    score_type: str = "rrf"
    model_version: str = ""


# ---------------------------------------------------------------- H09（纯）


def rrf_fuse(rankings: list[list[ScoredChunk]], k: int) -> list[ScoredChunk]:
    """H09：按 chunk_id 累加 1/(k+rank)，rank 从 1；同分稳定排序；保留原分。

    ★ RRF 分数只是**融合序**，不是置信度——不得拿它当相似度阈值用。
    """
    if k <= 0:
        raise TaskError("INVALID_ARGUMENT", f"rrf_k 必须为正，当前 {k}", transient=False)
    scores: dict[int, float] = {}
    keep: dict[int, ScoredChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            keep.setdefault(chunk.chunk_id, chunk)
    fused: list[ScoredChunk] = []
    for chunk_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        base = keep[chunk_id]
        fused.append(
            ScoredChunk(
                chunk_id=base.chunk_id,
                unit_id=base.unit_id,
                version=base.version,
                text=base.text,
                dense_score=base.dense_score,
                keyword_score=base.keyword_score,
                rrf_score=score,
                rerank_score=base.rerank_score,
            )
        )
    return fused


# ---------------------------------------------------------------- H10（纯）


def build_messages(
    history: list[dict[str, Any]],
    evidence: list[ScoredChunk],
    question: str,
    budget: int,
) -> list[dict[str, str]]:
    """H10：组装安全 Prompt（历史已由调用方过 H05 再授权——纯函数不读库）。

    - 证据编号 `[1]…[n]`，供模型引用、`citations` 事件对齐；
    - 历史从最新往回保留完整轮次，预算不足从最旧丢弃；
    - 系统约束与当前问题**永不截断**；预算装不下核心内容 → 受控拒绝。
    """
    system = (
        "你是企业知识库助手。只依据下方提供的证据回答问题；"
        "证据不足以回答时，明确说明知识库中没有相关内容。"
        "引用证据时在句末标注编号，如 [1]。不要编造证据之外的内容。"
    )
    evidence_block = "\n\n".join(
        f"[{index}] {chunk.text.strip()}" for index, chunk in enumerate(evidence, start=1)
    )
    core = len(system) + len(evidence_block) + len(question)
    if core + 64 > budget:
        raise TaskError(
            "PROMPT_BUDGET_EXCEEDED",
            "证据与问题超出上下文预算，请缩小召回规模",
            transient=False,
        )

    kept: list[dict[str, str]] = []
    used = core
    for turn in reversed(history):
        text = str(turn.get("text") or "")
        if not text:
            continue
        cost = len(text) + 8
        if used + cost > budget:
            break
        used += cost
        kept.append({"role": str(turn.get("role") or "user"), "content": text})
    kept.reverse()

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(kept)
    messages.append(
        {"role": "user", "content": f"参考证据：\n{evidence_block}\n\n问题：{question}"}
    )
    return messages


# ---------------------------------------------------------------- H08（IO）


async def retrieve_authorized(
    session: AsyncSession, ctx: Any, query: str, config: dict[str, Any]
) -> RetrievalResult:
    """H08：两路各 N 召回 → MySQL 版本复核 → authorize_units → 融合重排 Top-K。"""
    from app.engines.permission import authorize_units

    settings = config["settings"]
    vector_top_k = int(config.get("vector_top_k", 20))
    keyword_top_k = int(config.get("keyword_top_k", 20))
    answer_top_k = int(config.get("answer_top_k", 5))
    rrf_k = int(config.get("rrf_k", 60))

    # ---- 路径 A：向量召回 ----
    vectors, _usage, model_version = await embedding_provider.embed_batches([query], settings)
    milvus_hits = await vector_store.search_similar(
        settings, query_vector=vectors[0], top_k=vector_top_k
    )

    # ---- 路径 B：关键字召回（MySQL LIKE；倒排引擎未建，见模块注释）----
    keyword_hits = await _keyword_recall(session, query, keyword_top_k)

    # ---- 合并候选键 ----
    candidates: dict[tuple[int, int, int], dict[str, float | None]] = {}
    for hit in milvus_hits:
        key = (int(hit["unit_id"]), int(hit["version"]), int(hit["seq"]))
        candidates[key] = {"dense": float(hit.get("score") or 0.0), "keyword": None}
    for hit in keyword_hits:
        key = (int(hit["unit_id"]), int(hit["version"]), int(hit["seq"]))
        entry = candidates.setdefault(key, {"dense": None, "keyword": None})
        entry["keyword"] = float(hit.get("keyword_score") or 1.0)
    if not candidates:
        return RetrievalResult(result_type="no_evidence", model_version=model_version)

    # ---- 读回切片正文（一次 IN 查询，Python 侧按键匹配）----
    unit_ids = {key[0] for key in candidates}
    chunk_rows = (
        (
            await session.execute(
                select(Chunk).where(Chunk.unit_id.in_(unit_ids))
            )
        )
        .scalars()
        .all()
    )
    by_key = {
        (int(row.unit_id), int(row.version), int(row.seq)): row for row in chunk_rows
    }

    # ---- 版本复核（H08 第 2 步）：候选必须是"当前已索引版本"的切片 ----
    unit_rows = {
        int(row.id): row
        for row in (
            await session.execute(select(KnowledgeUnit).where(KnowledgeUnit.id.in_(unit_ids)))
        ).scalars()
    }
    denied_ids: list[int] = []
    unavailable_ids: list[int] = []
    valid: list[tuple[tuple[int, int, int], Any]] = []
    for key in candidates:
        unit = unit_rows.get(key[0])
        if unit is None or unit.is_deleted or not unit.enabled:
            # 单元不存在/已删除/停用：交给判定层归类（不在这里替它决定"无权"）。
            valid.append((key, unit))
            continue
        if (
            unit.index_status != "indexed"
            or unit.indexed_version is None
            or int(unit.indexed_version) != int(unit.content_version)
            or key[1] != int(unit.content_version)
        ):
            unavailable_ids.append(key[0])
            continue
        valid.append((key, unit))
    if not valid:
        return RetrievalResult(
            denied_ids=sorted(set(denied_ids)),
            unavailable_ids=sorted(set(unavailable_ids)),
            result_type="no_evidence",
            model_version=model_version,
        )

    # ---- 数据读权判定（复用 H04）----
    valid_unit_ids = sorted({key[0] for key, _unit in valid if _unit is not None})
    allowed_units, denied, unavailable, _versions = await authorize_units(
        session, ctx, valid_unit_ids
    )
    denied_ids.extend(denied)
    unavailable_ids.extend(unavailable)
    allowed_unit_set = set(allowed_units)
    if not allowed_unit_set:
        return RetrievalResult(
            denied_ids=sorted(set(denied_ids)),
            unavailable_ids=sorted(set(unavailable_ids)),
            result_type="access_restricted" if denied else "no_evidence",
            model_version=model_version,
        )

    # ---- 组装两路排名（仅有权候选）并融合 ----
    dense_ranking: list[ScoredChunk] = []
    keyword_ranking: list[ScoredChunk] = []
    seen: set[int] = set()
    for key, scores in candidates.items():
        row = by_key.get(key)
        if row is None or key[0] not in allowed_unit_set:
            continue
        chunk = ScoredChunk(
            chunk_id=int(row.id),
            unit_id=key[0],
            version=key[1],
            text=row.text,
            dense_score=scores["dense"],
            keyword_score=scores["keyword"],
        )
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        if scores["dense"] is not None:
            dense_ranking.append(chunk)
        if scores["keyword"] is not None:
            keyword_ranking.append(chunk)

    fused = rrf_fuse([dense_ranking, keyword_ranking], k=rrf_k)[:answer_top_k]
    if not fused:
        return RetrievalResult(
            denied_ids=sorted(set(denied_ids)),
            unavailable_ids=sorted(set(unavailable_ids)),
            result_type="no_evidence",
            model_version=model_version,
        )
    return RetrievalResult(
        allowed=fused,
        denied_ids=sorted(set(denied_ids)),
        unavailable_ids=sorted(set(unavailable_ids)),
        result_type="answered",
        top_score=fused[0].rrf_score,
        score_type="rrf",
        model_version=model_version,
    )


async def _keyword_recall(
    session: AsyncSession, query: str, top_k: int
) -> list[dict[str, Any]]:
    """关键字路（MySQL LIKE，倒排引擎未建——见模块注释）。

    简单分词：按非中文字符/标点切分，取长度 ≥2 的片段作 LIKE 条件（OR），
    命中即按片段长度给分（越长越具体）。这是工程折衷，不是检索质量声明。
    """
    import re

    terms = [
        term for term in re.split(r"[\s，。；：、！？（）()\[\]{}\"'“”·,.;:!?\-]+", query.strip())
        if len(term) >= 2
    ][:5]
    if not terms:
        return []
    conditions = [Chunk.text.like(f"%{term}%") for term in terms]
    rows = (
        await session.execute(
            select(Chunk.unit_id, Chunk.version, Chunk.seq, Chunk.text)
            .where(*conditions)
            .limit(top_k)
        )
    ).all()
    hits: list[dict[str, Any]] = []
    for unit_id, version, seq, text in rows:
        best = max((len(term) for term in terms if term in text), default=1)
        hits.append(
            {"unit_id": int(unit_id), "version": int(version), "seq": int(seq),
             "keyword_score": min(1.0, best / 20.0)}
        )
    return hits
