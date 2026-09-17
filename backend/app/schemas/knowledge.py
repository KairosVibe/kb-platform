"""M04 请求 DTO（API-CONTRACTS §8 F-04.01—F-04.09 与 §5 API-S04）。

响应体一律由服务层 dict + `ok()` 包装（与 org/ingest 路由一致），
这里只声明**请求形状**；约束（长度/取值）在服务层校验并映射为
422/409——DTO 只挡形状，不重复业务规则，避免两处规则漂移。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeUnitListQuery(BaseModel):
    """F-04.01 查询参数（由 Query 逐项注入，此模型仅作文档锚点）。"""


class UpdateMetadataRequest(BaseModel):
    """F-04.02：PATCH /api/knowledge-units/{unit_id}。"""

    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    expected_revision: int


class ChunkMutationRequest(BaseModel):
    """F-04.05：POST /api/knowledge-units/{unit_id}/chunk-mutations。"""

    chunk_id: int
    action: str = Field(pattern="^(edit|split|delete)$")
    text: str | None = None
    split_offset: int | None = None
    expected_revision: int


class SetEnabledRequest(BaseModel):
    """F-04.06：PUT /api/knowledge-units/{unit_id}/enabled。"""

    enabled: bool
    expected_revision: int


class DeleteUnitRequest(BaseModel):
    """F-04.07：DELETE /api/knowledge-units/{unit_id}。body 承载 expected_revision
    （首版统一 body，不用 If-Match——与 org 路由同款约定）。"""

    expected_revision: int


class UpdateAclRequest(BaseModel):
    """F-04.08：PUT /api/knowledge-units/{unit_id}/acl。四维整体提交（全量替换）。"""

    global_: bool = Field(alias="global")
    depts: list[int] = Field(default_factory=list)
    roles: list[int] = Field(default_factory=list)
    users: list[int] = Field(default_factory=list)
    expected_revision: int

    model_config = {"populate_by_name": True}
