"""向量库写入（Milvus standalone，pymilvus MilvusClient）。

M03 只需要**写路径**（H16 的 upsert）；检索（H08 两路召回）属 M05，这里不做。
集合 schema：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT64 auto_id 主键 | Milvus 自增；业务键是下面三个标量 |
| unit_id / version / seq | INT64 | 与 MySQL `chunk(unit_id,version,seq)` 唯一键同构 |
| vector | FLOAT_VECTOR(dim) | 维度由配置冻结（D-03：1024） |

**写入策略：先按 `(unit_id, version)` 删后插，而不是依赖 upsert 主键**。
原因：业务主键 (unit,version,seq) 不是一个 INT64，无法直接做 Milvus 主键；
"删旧插新"以版本为单位幂等——重试重跑同一版本不会产生重复向量（H16"同版本
复用持久切片"在向量侧的对应物）。
"""

from __future__ import annotations

from typing import Any

from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient

from app.core.errors import TaskError


def _connect(config: Any) -> MilvusClient:
    return MilvusClient(uri=config.milvus_uri, timeout=30)


def ensure_collection(client: MilvusClient, name: str, dim: int) -> None:
    """集合不存在则创建（幂等）。已存在但维度不符 → 永久错误（换空间必须换代际）。"""
    if client.has_collection(name):
        described = client.describe_collection(name)
        vector_field = next(
            (f for f in described.get("fields", []) if f.get("name") == "vector"), None
        )
        if vector_field and vector_field.get("params", {}).get("dim") != dim:
            raise TaskError(
                "COLLECTION_DIM_MISMATCH",
                f"既有 collection {name!r} 维度 {vector_field['params'].get('dim')} != 配置 {dim}",
                transient=False,
            )
        return

    schema = CollectionSchema(
        fields=[
            FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema("unit_id", DataType.INT64),
            FieldSchema("version", DataType.INT64),
            FieldSchema("seq", DataType.INT64),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=dim),
        ],
        description="知识切片向量（unit:version:seq 对应 MySQL chunk 表）",
    )
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)


async def upsert_version_chunks(
    config: Any, *, rows: list[dict[str, Any]]
) -> int:
    """写入一个版本的向量（先删后插），返回写入条数。

    `rows`：`[{unit_id, version, seq, vector}]`。空列表直接返回 0（合法：
    清洗后没有可索引切片的情形在 H20 已拦截，这里防御空集）。
    """
    if not rows:
        return 0
    client = _connect(config)
    try:
        name = config.milvus_collection
        ensure_collection(client, name, int(config.embed_dim))
        unit_id = rows[0]["unit_id"]
        version = rows[0]["version"]
        client.delete(collection_name=name, filter=f"unit_id == {unit_id} and version == {version}")
        inserted = client.insert(
            collection_name=name,
            data=[
                {
                    "unit_id": row["unit_id"],
                    "version": row["version"],
                    "seq": row["seq"],
                    "vector": row["vector"],
                }
                for row in rows
            ],
        )
        return int(inserted.get("insert_count", 0)) if isinstance(inserted, dict) else len(rows)
    except TaskError:
        raise
    except Exception as exc:
        # Milvus 不可用/超时属瞬时失败：任务回 retry_wait，H14 到期重试。
        raise TaskError("VECTOR_WRITE_FAILED", f"向量写入失败：{exc}", transient=True) from exc
    finally:
        client.close()


async def search_similar(config: Any, *, query_vector: list[float], top_k: int) -> list[dict[str, Any]]:
    """向量检索（H08 路径 A）。返回 `[{unit_id, version, seq, score}]`。

    ★ Milvus 检索**只做预筛**：这里不带授权条件（标量过滤授权不可靠且难维护），
      授权判定统一在 H08 的 authorize_units 复核——向量层永远不是权限边界。
    ★ async 签名与调用方（H08）一致：Milvus 客户端是同步的，包在 async 里是
      为了保持"引擎只 await 提供方"的统一调用形态（首轮实测：同步 def 被 await
      直接 TypeError——签名一致性不是装饰）。
    """
    client = _connect(config)
    try:
        name = config.milvus_collection
        if not client.has_collection(name):
            return []
        results = client.search(
            collection_name=name,
            data=[query_vector],
            limit=top_k,
            output_fields=["unit_id", "version", "seq"],
        )
        hits: list[dict[str, Any]] = []
        for batch in results:
            for hit in batch:
                entity = hit.get("entity", {})
                hits.append(
                    {
                        "unit_id": int(entity.get("unit_id", 0)),
                        "version": int(entity.get("version", 0)),
                        "seq": int(entity.get("seq", 0)),
                        "score": float(hit.get("distance", 0.0)),
                    }
                )
        return hits
    except TaskError:
        raise
    except Exception as exc:
        raise TaskError("VECTOR_SEARCH_FAILED", f"向量检索失败：{exc}", transient=True) from exc
    finally:
        client.close()


def count_version_chunks(
    config: Any, *, unit_id: int, version: int, expected: int | None = None
) -> int:
    """统计某版本在向量库中的真实条数（H16"核对数量"的依据）。

    ★ 以库内实数为准，不信 insert 响应回执——回执只说明"请求被接受"，
      不说明"现在库里有多少"。

    ★ 为什么可以带 `expected` 轮询：Milvus 默认 Bounded 一致性，**刚插入的数据
      对 query 不保证立即可见**（本轮真实链路补验实测：insert 成功但立即计数为 0，
      被误判成写入不完整）。带期望值做有界退避轮询，把"还没可见"与"真的丢了"
      区分开——前者是最终一致性的正常延迟，后者才是 H16 该报的 mismatch。
    """
    import time

    client = _connect(config)
    try:
        name = config.milvus_collection
        if not client.has_collection(name):
            return 0
        count = 0
        for attempt in range(6 if expected is not None else 1):
            rows = client.query(
                collection_name=name,
                filter=f"unit_id == {unit_id} and version == {version}",
                output_fields=["count(*)"],
            )
            count = int(rows[0]["count(*)"]) if rows else 0
            if expected is None or count == expected:
                return count
            time.sleep(1.0 + attempt)
        return count
    except TaskError:
        raise
    except Exception as exc:
        raise TaskError("VECTOR_QUERY_FAILED", f"向量计数失败：{exc}", transient=True) from exc
    finally:
        client.close()
