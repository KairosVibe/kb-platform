"""部署配置（12-factor：全部来自环境变量，可热改业务参数走 `config_revision` 表）。

对应 FUNCTION-MAP.md §1（公共类型 `config（Provider）`）与 F-09.02、PRD BC-09.02。
生成/重排模型、阈值、TopK、挖掘周期等**可热改参数**由 `config_revision` 表承载
（DATA-CONTRACTS §2：id/patch/snapshot/actor_id/created_at，问答绑定其 revision），
不在此处；本文件只放"部署期决定、运行期不变"的配置。

★ 勘误（2026-09-16，M1-6）：本文旧版把该表写作 `sys_config`，但 `sys_config` 在
  PRD / FUNCTION-MAP / DATA-CONTRACTS 中均**不存在**，属文档编号漂移，已按
  DATA-CONTRACTS §2 更正为 `config_revision`。

★ embedding 模型属于**部署期决定且不可热切换**：模型版本串（provider+model+dimension）
  变化即等于更换向量空间，必须新建索引代际并重建后切流，禁止在同一 collection 内混查
  （PRD BC-09.02、API-CONTRACTS §6 → 409 REINDEX_REQUIRED；审计 PA-10）。

★ 部署期环境变量的**权威声明处是 DEPLOYMENT §9.2**（含向量库双形态约束、D-08 的中间件
  地址与 `.env` 纪律）。本文件是该节的实现，两者必须一致：**改默认值前先改那一节**。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全量部署配置。缺失必填项时启动即失败（fail-fast），不留到运行期。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 运行环境 ----
    app_env: Literal["dev", "prod"] = "dev"
    app_name: str = "kb-platform"
    api_prefix: str = "/api"

    # ---- 数据库 ----
    database_url: str = Field(
        default="mysql+aiomysql://kb:kb_pass@127.0.0.1:3306/kb_platform?charset=utf8mb4",
        description="SQLAlchemy async DSN；测试可覆盖为 sqlite+aiosqlite:///:memory:",
    )
    db_echo: bool = False

    # ---- JWT ----
    jwt_secret: SecretStr = Field(
        default=SecretStr("dev-only-secret-change-me-in-production"),
        description="生产必须覆盖；长度 <32 时启动失败",
    )
    jwt_algorithm: str = "HS256"
    access_ttl_min: int = 120          # access token 2h
    refresh_ttl_days: int = 7          # refresh token 7d

    # ---- 登录限流 ----
    login_rate_per_min: int = 10       # 同 IP 每分钟登录尝试上限

    # ---- DashScope 模型接入 ----
    dashscope_api_key: SecretStr = SecretStr("")
    # 生成与向量化走 OpenAI 兼容接口
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # rerank 必须走原生接口：compatible-mode 不支持 rerank（返回 404）
    # 依据：REUSE-MATRIX.md R02（重排 H12 的适配差异：两种请求结构 + 空文本索引映射）
    rerank_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    rerank_model: str = "qwen3-rerank"
    llm_model: str = "qwen-plus"
    llm_timeout_s: int = 60

    # ---- 向量化 ----
    # ★ 以下三项是**部署期决定**，不是运行期降级开关：切换 provider / model / dimension
    #   等于更换向量空间，必须新建索引代际并全量重建后再切流（PRD BC-09.02）。
    #   禁止"配额耗尽时临时切到本地模型继续查询旧索引"——那会把两套向量混进同一个
    #   collection，相似度不再有意义（审计 PA-10）。
    embed_provider: Literal["qwen", "local_bge"] = "qwen"
    embed_model: str = "text-embedding-v3"
    embed_dim: int = 1024              # 必须与 Milvus collection schema 一致
    embed_batch_size: int = 10         # 单请求条数（DashScope 限制）
    local_bge_dir: str = "./models"    # provider=local_bge 时的本地模型目录

    # ---- 向量库（双形态）----
    # ★ 形态与地址必须成对一致（校验见 _check_vector_store_form，依据 DEPLOYMENT §9.2）。
    #   默认取 standalone：milvus_lite 仅支持 Linux/macOS，而本机开发平台是 Windows，
    #   一个"在本机不可能工作"的默认值是陷阱而非便利。
    vector_backend: Literal["milvus_standalone", "milvus_lite"] = "milvus_standalone"
    milvus_uri: str = "http://127.0.0.1:19530"   # standalone: http(s)://host:port；lite: 文件路径
    milvus_collection: str = "kb_chunks"
    faq_collection: str = "faq_q"

    # ---- 图库（Neo4j）----
    # D-08：中间件由 VM 承载，宿主机经局域网连接（DEPLOYMENT §9.2）。
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = "neo4j"

    # ---- 上传 ----
    upload_max_mb: int = 20
    upload_dir: str = "./data/uploads"
    allowed_ext: tuple[str, ...] = (".pdf", ".docx", ".md", ".txt")

    # ---- 成本控制 ----
    daily_call_limit: int = 2000       # 超限记录并告警

    # ---- 分页 ----
    page_size_default: int = 20
    page_size_max: int = 100

    @field_validator("jwt_secret")
    @classmethod
    def _check_jwt_secret(cls, v: SecretStr) -> SecretStr:
        """生产环境强制要求足够长度的密钥。

        弱密钥直接导致 token 可被伪造，属安全红线，故在此 fail-fast 而非运行期告警。
        """
        raw = v.get_secret_value()
        if len(raw) < 32 and raw != "dev-only-secret-change-me-in-production":
            raise ValueError("JWT_SECRET 长度必须 >= 32 字符")
        return v

    @field_validator("embed_dim")
    @classmethod
    def _check_embed_dim(cls, v: int) -> int:
        """维度必须为正：写错会导致 Milvus schema 与向量不一致，且错误在检索期才暴露。"""
        if v <= 0:
            raise ValueError("EMBED_DIM 必须为正整数")
        return v

    @model_validator(mode="after")
    def _check_vector_store_form(self) -> Self:
        """形态与地址必须一致，否则会连到错误的形态（DEPLOYMENT §9.2）。

        ★ 为什么要在启动期拦：`vector_backend` 与 `milvus_uri` 是两个字段，
          只改其中一个**不会**自然报错——它会一路连到错误的形态，直到第一次检索
          才以"连不上/空结果"的形式出现，归因成本很高。此前这条约束只写在文档
          提醒里（"两个配置必须一起改"），靠人记住；这里把它前移为启动期失败。

        Neo4j 的 URI scheme 同批校验：`http://...` 这类地址在 bolt 驱动下同样不会
        在启动期报错，而是连接期超时。
        """
        uri = self.milvus_uri.strip()
        is_http = uri.startswith(("http://", "https://"))
        if self.vector_backend == "milvus_standalone" and not is_http:
            raise ValueError(
                "VECTOR_BACKEND=milvus_standalone 要求 MILVUS_URI 为 http(s):// 地址，"
                f"当前为 {uri!r}"
            )
        if self.vector_backend == "milvus_lite" and is_http:
            raise ValueError(
                "VECTOR_BACKEND=milvus_lite 要求 MILVUS_URI 为文件路径（lite 仅 Linux/macOS 可用），"
                f"当前为 {uri!r}"
            )
        if not self.neo4j_uri.startswith(("bolt://", "neo4j://")):
            raise ValueError(
                f"NEO4J_URI 必须以 bolt:// 或 neo4j:// 开头，当前为 {self.neo4j_uri!r}"
            )
        return self

    @property
    def embedding_model_version(self) -> str:
        """向量空间的唯一标识（写入 `knowledge_version.embedding_model_version`）。

        ★ 该串一旦变化，就说明新老向量不在同一空间：必须新建索引代际并重建，
          不得复用既有向量、更不得在同一 collection 内混查（PRD BC-09.02）。
          索引/检索侧据此判断"现有向量能否直接复用"。
        """
        return f"{self.embed_provider}:{self.embed_model}:{self.embed_dim}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。lru_cache 保证同一个 Settings 实例贯穿全应用。

    注意：缓存后不可变；测试需覆盖配置时用 get_settings.cache_clear()。
    """
    return Settings()
