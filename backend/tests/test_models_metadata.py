"""实体元数据体检（**同步用例，不需要数据库连接**）。

为什么需要这组用例：初始迁移的 DDL 是"渲染出来才能看见"的，人工审阅容易漏。
本文件把那次人工审阅里真正抓到问题的几项固化成断言——**其中 `DATETIME(6)` 一条
本可以在生成迁移时就直接失败**（当时的 `DateTime(6)` 在 MySQL 方言下渲染成 `DATETIME`，
微秒被静默丢弃，见 WORKLOG 问题 #38）。

边界（必须诚实声明）：**本文件不验证任何数据库行为**。唯一键竞争、外键删除限制、
`CHECK` 是否真的拦住脏数据、行锁与租约、utf8mb4 排序规则的实际比较结果，都只能在
真实 MySQL 8.0.26 上验证（DATA-CONTRACTS §5 明确禁止用元数据体检或 SQLite 替代）。
本文件只回答一个问题：**我们声明出来的结构，是否真的会被渲染成设计的样子**。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.models import ALL_MODELS

# DATA-CONTRACTS §2 实体字典的**全部 38 张表**（M1—M8 + 运维一致性 + M03 上传受理登记）。
# ★ 本清单必须与 `models/__init__.py` 的 `ALL_MODELS` 同步增删：
#   它是"契约声明的表集"与"实际注册的表集"做差集的那一端，
#   漏更新会让集合比对**在两侧同时为零**时也通过（清单短了，比对就失去意义）。
EXPECTED_TABLES = {
    # 身份与会话
    "department",
    "user",
    "role",
    "role_permission",
    "user_role",
    "auth_session",
    "refresh_token",
    # 知识与导入
    "knowledge_unit",
    "knowledge_acl_department",
    "knowledge_acl_role",
    "knowledge_acl_user",
    "knowledge_version",
    "chunk",
    "index_task",
    "cleanup_task",
    "upload_batch",
    "upload_item",
    # 会话与审计
    "chat_session",
    "chat_request",
    "chat_message",
    "message_source",
    "chat_event",
    "qa_audit",
    "model_call_usage",
    # FAQ 与沉淀
    "faq",
    "faq_source",
    "mining_run",
    "mining_consumption",
    "question_cluster",
    "cluster_member",
    "faq_draft_job",
    # 缺口闭环
    "knowledge_gap",
    "gap_request",
    "supplement_task",
    # 运维
    "config_revision",
    "operation_log",
    "outbox_event",
    "idempotency_record",
}

#: 关联表：**故意没有** created_at/updated_at（DATA-CONTRACTS §1"除关联表外具有…"）。
ASSOCIATION_TABLES = {
    "role_permission",
    "user_role",
    "knowledge_acl_department",
    "knowledge_acl_role",
    "knowledge_acl_user",
    "message_source",
    "mining_consumption",
    "cluster_member",
    "gap_request",
    "faq_source",
}

#: append-only 表：只有 created_at，**不应有 updated_at/revision**
#: （记录"发生过什么"而非"现在是什么"，可原地修改的历史不是审计）。
APPEND_ONLY_TABLES = {"config_revision", "operation_log", "idempotency_record", "chat_event"}


def _mysql_ddl(table_name: str) -> str:
    """按 MySQL 方言渲染单表 DDL（不连接数据库）。"""
    table = Base.metadata.tables[table_name]
    return str(CreateTable(table).compile(dialect=mysql.dialect()))


def test_all_expected_tables_registered() -> None:
    """全部 38 张表都注册进 metadata。

    ★ 这条能拦住"新增实体忘了在 models/__init__.py 导入"——那种漏法不报错，
    只会让迁移少一张表，直到集成测试或线上才炸。
    """
    registered = set(Base.metadata.tables)
    assert registered == EXPECTED_TABLES, (
        f"缺失：{sorted(EXPECTED_TABLES - registered)}；"
        f"多出：{sorted(registered - EXPECTED_TABLES)}"
    )
    assert len(ALL_MODELS) == len(EXPECTED_TABLES)


def test_foreign_keys_all_resolve() -> None:
    """全部外键可解析。`sorted_tables` 在存在悬空 FK 时会抛 `NoReferencedTableError`。"""
    ordered = list(Base.metadata.sorted_tables)
    assert len(ordered) == len(EXPECTED_TABLES)
    # 被引用的表必须排在引用它的表之前（自引用 department 例外，同级）。
    position = {t.name: i for i, t in enumerate(ordered)}
    assert position["user"] < position["auth_session"]
    assert position["auth_session"] < position["refresh_token"]
    assert position["knowledge_version"] < position["chunk"]


def test_naming_convention_applied() -> None:
    """约束/索引名必须带约定前缀（DATA-CONTRACTS §5：不用数据库默认名）。

    理由：Alembic 的 downgrade 需要按名字删约束；用 MySQL 默认名（`t_user_ibfk_1`
    这类）会让迁移不可逆，且不同环境名字不一致。
    """
    for table in Base.metadata.sorted_tables:
        assert table.primary_key.name.startswith("pk_"), table.name
        for fk in table.foreign_keys:
            assert fk.constraint.name.startswith("fk_"), (table.name, fk.constraint.name)
        for index in table.indexes:
            assert index.name.startswith("ix_"), (table.name, index.name)

    # MySQL 会为"列级 unique=True"生成表级 UniqueConstraint，同样要走命名约定。
    uq_names = {
        c.name
        for table in Base.metadata.sorted_tables
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert uq_names, "预期至少存在若干唯一约束"
    assert all(n.startswith("uq_") for n in uq_names), sorted(uq_names)


def test_common_columns_follow_contract() -> None:
    """公共列按 DATA-CONTRACTS §1 分布：非关联表有双时间列，append-only 表只有 created_at。"""
    for name in EXPECTED_TABLES:
        columns = set(Base.metadata.tables[name].columns.keys())
        if name in ASSOCIATION_TABLES:
            assert "created_at" not in columns, name
            assert "updated_at" not in columns, name
        elif name in APPEND_ONLY_TABLES:
            assert "created_at" in columns, name
            assert "updated_at" not in columns, f"{name}：审计/幂等记录不应可原地修改"
            assert "revision" not in columns, name
        else:
            assert {"created_at", "updated_at"} <= columns, name


def test_microsecond_precision_survives_mysql_dialect() -> None:
    """★ 时间列必须是 `DATETIME(6)`，不能退化成秒精度的 `DATETIME`。

    泛型 `DateTime(6)` 在 MySQL 方言下**不保留精度**（实测渲染为 `DATETIME`）。
    丢掉微秒不是"少几位小数"：同秒内的 `consumed_at`、`last_seq` 排序会变得不确定，
    `lease_until` 与令牌到期比较也会失真——这些正是并发正确性的依据。
    """
    for table_name, column_name in (
        ("auth_session", "expires_at"),
        ("refresh_token", "consumed_at"),
        ("index_task", "lease_until"),
        ("operation_log", "created_at"),
        ("department", "created_at"),
    ):
        ddl = _mysql_ddl(table_name)
        assert f"{column_name} DATETIME(6)" in ddl, (
            f"{table_name}.{column_name} 未渲染为 DATETIME(6)：\n{ddl}"
        )


def test_no_plain_datetime_anywhere() -> None:
    """全库不应存在不带精度的 DATETIME（防止后续新增列时漏用 `UTCDateTime`）。"""
    offenders: list[str] = []
    for table_name in EXPECTED_TABLES:
        for line in _mysql_ddl(table_name).splitlines():
            if "DATETIME" in line and "DATETIME(6)" not in line:
                offenders.append(f"{table_name}: {line.strip()}")
    assert not offenders, offenders


def test_identity_collation_is_binary() -> None:
    """身份唯一性字段必须用二进制排序规则。

    默认排序规则（`utf8mb4_0900_ai_ci` 等）大小写不敏感，会把 `Admin` 与 `admin`
    判为同一身份——**安全身份的唯一性不能由排序规则决定**（DATA-CONTRACTS §1）。
    """
    for table_name, column_name in (("user", "username_norm"), ("role", "name_norm")):
        column = Base.metadata.tables[table_name].c[column_name]
        assert column.type.collation == "utf8mb4_bin", (
            f"{table_name}.{column_name} 排序规则为 {column.type.collation!r}"
        )
        assert f"{column_name} VARCHAR(64) COLLATE utf8mb4_bin" in _mysql_ddl(table_name)


def test_global_column_is_is_global() -> None:
    """四维之一 `global` 在实现中叫 `is_global`（FUNCTION-MAP §1 名称映射表已登记）。

    `GLOBAL` 是 MySQL 保留字：裸列名必须反引号转义，漏写就是语法错误。
    """
    columns = set(Base.metadata.tables["knowledge_unit"].columns.keys())
    assert "is_global" in columns
    assert "global" not in columns
    assert "is_global BOOL NOT NULL" in _mysql_ddl("knowledge_unit")


def test_index_status_check_matches_prd() -> None:
    """`index_status` 三态来自 PRD §1.2 第 1 条，且**不含 `failed`**。

    `failed` 是任务的结局（`index_task.status`），不是单元状态。把两者合并会让
    "这个单元到底能不能被检索"失去唯一答案。
    """
    ddl = _mysql_ddl("knowledge_unit")
    assert "ck_knowledge_unit_index_status_values" in ddl
    assert "'pending', 'indexed', 'stale'" in ddl
    assert "failed" not in ddl

    task_ddl = _mysql_ddl("index_task")
    assert "ck_index_task_status_values" in task_ddl
    # stage 与 status 正交：stage 允许 NULL，status 不允许。
    assert "ck_index_task_stage_values" in task_ddl
    assert "stage IS NULL OR stage IN" in task_ddl


def test_chunk_has_composite_fk_to_version() -> None:
    """切片必须通过**复合外键**绑定到版本（DATA-CONTRACTS §2"复合FK对应版本"）。

    若只绑 `unit_id`，切片就可能挂在不存在的版本上，检索时按 `(version, seq)`
    推导向量键会指向空数据。
    """
    ddl = _mysql_ddl("chunk")
    assert "FOREIGN KEY(unit_id, version) REFERENCES knowledge_version (unit_id, version)" in ddl
    assert "uq_chunk_unit_id_version_seq" in ddl


def test_audit_columns_are_snapshots_not_secrets() -> None:
    """审计表只应有脱敏快照列，且**不含任何口令/令牌原文列**（DATA-CONTRACTS §2）。"""
    for table_name in ("operation_log", "config_revision"):
        columns = set(Base.metadata.tables[table_name].columns.keys())
        assert not {c for c in columns if "password" in c or "secret" in c or "token" in c}

    refresh_columns = set(Base.metadata.tables["refresh_token"].columns.keys())
    # 只允许哈希列，不允许 token 原文列。
    assert "token_hash" in refresh_columns
    assert "token" not in refresh_columns


def test_uuid_columns_render_as_char32_on_mysql() -> None:
    """记录 `Uuid` 在 MySQL 上的实际形态：`CHAR(32)`（**无连字符**的 32 位十六进制）。

    这条断言的价值在于"写死一个容易被误解的事实"：运维直接用 SQL 查
    `auth_session.id` 时，必须用 32 位无连字符形式，用 `uuid4()` 的带连字符
    字符串去比对会查不到。
    """
    ddl = _mysql_ddl("auth_session")
    assert "id CHAR(32) NOT NULL" in ddl


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_every_table_renders_on_mysql(table_name: str) -> None:
    """每张表都能在本机渲染出 MySQL DDL（防止出现方言不支持的类型组合）。"""
    ddl = _mysql_ddl(table_name)
    # 保留字表名会被加反引号，因此两种形式都接受（见下一条用例）。
    assert f"CREATE TABLE {table_name} (" in ddl or f"CREATE TABLE `{table_name}` (" in ddl
    assert "ENGINE" not in ddl  # 引擎/字符集在**建库时**设定，不在表上重复声明


def test_reserved_word_table_name_is_quoted() -> None:
    """`role` 是 MySQL 8 保留字：DDL 必须带反引号，裸写会语法错误。

    ★ 这条断言同时防止一次"过度修正"：不要因为 `role` 需要引号就给所有表都加引号——
      `user` 并非保留字，SQLAlchemy 只在必要时引用。若有人把引用方式改成全局强制，
      这条用例会失败并强迫其确认这是有意的。
    """
    assert "CREATE TABLE `role` (" in _mysql_ddl("role")
    assert "CREATE TABLE user (" in _mysql_ddl("user")
    # MySQL 8 保留字列名同样会被引用：mining_run.trigger（见 faq.py 注释）。
    assert "`trigger` VARCHAR(64) NOT NULL" in _mysql_ddl("mining_run")


# ---------------------------------------------------------------- M05—M08 专项

def test_qa_audit_primary_key_is_request_id_only() -> None:
    """★ 审计表主键必须**只有** `request_id`，不得另加自增 id。

    多一个自增主键就允许"同一请求两行审计"，"一次请求一条审计"便不再是数据库保证的
    性质，而只能靠应用纪律——这正是审计最容易出问题的地方。
    """
    table = Base.metadata.tables["qa_audit"]
    assert [c.name for c in table.primary_key.columns] == ["request_id"]
    assert "id" not in table.columns


def test_faq_has_no_independent_acl_table() -> None:
    """★ FAQ **不得**有独立 ACL 表（DATA-CONTRACTS §2："FAQ不保存可扩张的独立ACL替代来源权限"）。

    若为了"让某些人看到某条 FAQ"而加一张 faq_acl，四维权限就出现了第二个事实源，
    来源撤权后 FAQ 仍可能直出。本用例把这条约定固化成可执行断言。
    """
    offenders = [t for t in Base.metadata.tables if t.startswith("faq_acl")]
    assert not offenders, f"不应存在 FAQ 独立 ACL 表：{offenders}"


def test_faq_source_prevents_mixed_versions() -> None:
    """`faq_source` 主键为 `(faq_id, unit_id)`，版本是属性而非键 → 混版本发布不可能。"""
    table = Base.metadata.tables["faq_source"]
    assert [c.name for c in table.primary_key.columns] == ["faq_id", "unit_id"]
    ddl = _mysql_ddl("faq_source")
    assert "FOREIGN KEY(unit_id, version) REFERENCES knowledge_version (unit_id, version)" in ddl


def test_chat_event_primary_key_and_seq_guard() -> None:
    """事件表主键即去重键：`(request_id, seq)`，且 `seq > 0` 由 CHECK 拦住。"""
    table = Base.metadata.tables["chat_event"]
    assert [c.name for c in table.primary_key.columns] == ["request_id", "seq"]
    ddl = _mysql_ddl("chat_event")
    assert "CHECK (seq > 0)" in ddl
    # heartbeat 不入库：事件集合里不应出现它（它没有 seq，留着会污染游标语义）。
    assert "heartbeat" not in ddl


def test_chat_request_idempotency_key_scope() -> None:
    """幂等键按 `(user_id, client_request_id)` 唯一，而不是全局唯一。"""
    ddl = _mysql_ddl("chat_request")
    assert "uq_chat_request_user_id_client_request_id" in ddl


def test_gap_department_key_defaults_to_zero() -> None:
    """`department_key` 默认 0 表示"无部门"（不用 NULL：唯一索引对 NULL 不去重）。"""
    column = Base.metadata.tables["knowledge_gap"].c.department_key
    assert column.nullable is False
    assert column.server_default is not None
    assert "uq_knowledge_gap_department_key_fingerprint" in _mysql_ddl("knowledge_gap")


def test_faq_draft_job_uses_natural_key() -> None:
    """`faq_draft_job` 用 `job_key` 作自然主键（契约未列 id）。"""
    table = Base.metadata.tables["faq_draft_job"]
    assert [c.name for c in table.primary_key.columns] == ["job_key"]


def test_message_source_binds_to_version_and_chunk() -> None:
    """引用同时绑定到**版本**与**切片**两个层级。"""
    ddl = _mysql_ddl("message_source")
    assert "FOREIGN KEY(unit_id, version) REFERENCES knowledge_version (unit_id, version)" in ddl
    assert "fk_message_source_chunk_id_chunk" in ddl
