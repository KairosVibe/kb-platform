"""模型与存储提供方（对应 FUNCTION-MAP §4 的 `providers.*` 基础函数）。

契约层：公开符号必须已登记 FUNCTION-MAP。
- `embedding.embed_batches` → H11
- `vector_store` 的写入函数是 H16 的外部副作用承载（H16 本身登记在 `app/tasks/version_store.py`）
"""

from app.providers import embedding, vector_store

__all__ = ["embedding", "vector_store"]
