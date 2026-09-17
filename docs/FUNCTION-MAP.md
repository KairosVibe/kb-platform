# 函数级拆解（FUNCTION MAP）· 知识库管理平台

> R3 定位与交付：通用知识管理与 AI 问答平台，用于真实工程落地及简历展示；不转为电商/售后专用产品。继承 R2 功能编号和安全契约。当前只修订文档，尚未确认进入开发。详见 [交付、评测与文档确认](DELIVERY-EVALUATION.md)。

版本：2026-09-16 R3；设计签名，不代表实现存在或已通过测试。

## 0. 架构与规则

1. 每个F编号映射PRD同编号US及AC；本版替代旧函数图谱，取消extra_perms放宽、隐式读权和重连重生成。
2. 后端IO函数async，算法纯函数；前端TypeScript。签名采用跨语言记法，null在Python为None，结构化返回实现为Pydantic DTO/类型化值对象。
3. 每个模块服务在backend/app/services/<module>.py；纯授权和检索算法在engines；Provider在providers；持久任务在tasks；前端composables与页面对应。delivery的前端/交付操作不强行创建后端服务。
4. 路由仅DTO校验→H01当前身份→H02功能检查→服务→安全响应；服务执行对象和数据授权。输入中的ctx不能由客户端body指定。
5. DB session/文件存储/Provider依赖由构造或请求作用域注入，不在每条业务签名重复列出。写入与operation_log短事务一致；网络调用事务外；跨存储通过持久任务恢复。
6. 旧代码中权限及测试期望需迁移，不能将旧31项测试通过当本版验收通过。

R3 契约补齐：[API 契约](API-CONTRACTS.md)、[数据契约](DATA-CONTRACTS.md)、[格式与验收](FORMAT-ACCEPTANCE.md)、[图册](diagrams/README.md)和[ADR-0003](adr/ADR-0003-contract-closure.md)。下列签名为业务服务设计，传输层字段与默认值按 API 契约，逻辑持久实体按数据契约；均非已实现声明。

## 1. 公共类型

- UserCtx：user_id:int、session_id:UUID、dept_id:int|null、role_ids:set[int]、permission_codes:set[str]、identity_revision:int；服务端构造，无is_super读权字段。
- UUID：标准UUID字符串，单次逻辑操作幂等键；ID均为正整数且不超过JavaScript安全整数上限。
- SecretStr：敏感值，禁止进入日志、repr与普通响应。
- UploadFile：filename:str、content_type:str、size:int、stream:异步字节流；不信任客户端路径与类型。
- SourceRef：unit_id:int、version:int、chunk_id:int|null。
- UnitState：unit_id、global、department_ids、role_ids、user_ids、enabled、is_deleted、content_version、indexed_version、index_status、acl_version。**判定输入类型不含 creator_id**——创建者信息只存在于数据行与审计，不进入授权输入，从类型上排除"创建者旁路"的可能。
- DenyReason：内部拒绝原因码 DENY_DEFAULT|DENY_DEPT|DENY_ROLE|DENY_USER|DENY_DISABLED|DENY_DELETED；**仅用于内部判定分类与受控审计，禁止下发客户端**。
- ClientReasonCode：**客户端可见**受限原因码，当前仅 ACCESS_RESTRICTED；新增成员前必须先修订 DESIGN_REVISION §2.2。
- RestrictedNotice：reason_code:ClientReasonCode、message:str；结构固定为这两个字段，不含受限 ID、标题、摘要、部门名单或数量。
- 固定受限提示：PARTIAL_RESTRICTED_NOTICE="部分参考资料因权限受限无法展示"（DESIGN_REVISION §2.2 原文，不得改写）；ACCESS_RESTRICTED_NOTICE（全部无权终态；契约只要求"固定受限提示"未固定文案，实现取"该问题超出当前权限范围，无法作答"，不得含数量）。
- FilterResult：allowed_ids:list[int]、denied_ids:list[int]、has_denied:bool；**只含单元 ID**，denied_ids 仅供受控审计，不得下发客户端。
- Usage：prompt_tokens:int|null、completion_tokens:int|null、embedding_tokens:int|null、rerank_units:int|null、status:known|unknown|not_applicable、call_ids:list[str]。
- AnswerResult：result_type:answered|faq_hit|access_restricted|no_evidence|low_confidence|service_error、answer:str、sources:list[SourceRef]、recall_ids/allowed_ids/denied_ids:list[int]、duration_ms:int、first_token_ms:int|null、error_code:str|null。
- SseEvent：request_id:int、seq:int、event:meta|delta|citations|denied|done|error、payload:dict。heartbeat为非持久心跳，无seq；客户端不据此推进游标。
- ScoredChunk：chunk_id、unit_id、version、text、dense_score|null、keyword_score|null、rrf_score|null、rerank_score|null。
- RetrievalResult：allowed:list[ScoredChunk]、denied_ids:list[int]、unavailable_ids:list[int]、result_type、top_score|null、score_type、model_version。
- TaskLease：task_id、lease_token、lease_until、worker_id、target_version。
- EmbeddedChunk：unit_id、version、seq、text、vector:list[float]、location:Location。
- ServerPath：服务端验证的存储绝对路径，禁止接受客户端任意路径。
- Location：page_no:int|null、start_offset:int、end_offset:int、original_offset:int|null。
- CancelSignal：cancelled:bool、reason:str|null，可协作取消对象。
- ModelDelta：text:str、usage:Usage|null、finish_reason:str|null。
- MiningBatch：run_id、pipeline_version、lease_token、log_ids:list[int]、questions:list[QuestionVector]。
- QuestionVector：log_id、question、vector:list[float]、dept_id、sources:list[SourceRef]。
- QuestionCluster：cluster_id、representative、members:list[QuestionVector]、frequency、sources、score。
- FaqDraft：job_key、question、answer、sources、cluster_id、confidence、model_version。
- Json：合法JSON值；实际业务字段必须符合对应签名，不允许任意透传。
- ByteReader：浏览器ReadableStreamDefaultReader<Uint8Array>。
- BrowserEntry：浏览器拖拽文件/目录条目，只在前端存在。
- config（Provider）：revision:int、endpoint:允许地址、secret_ref:str、model:str、dimension:int|null、timeout_seconds:int、max_retries:int。**embedding 侧须在配置中固化"模型版本串"**（provider+model+dimension，落到 `knowledge_version.embedding_model_version` 与 `index_generation`）：该串变化即视为**换向量空间**，必须新建索引代际并重建，禁止在同一 collection 内混查（PRD BC-09.02、API-CONTRACTS §6 → 409 REINDEX_REQUIRED）。**不存在"配额耗尽时热降级到本地模型"的路径**——`local_bge` 只是另一套部署配置，切换同样必须重建索引。
- config（Retrieval）：vector_top_k=20、keyword_top_k=20、max_per_route=100、answer_top_k=5、rrf_k=60、revision:int。
- 配置patch白名单：llm_model、embedding_model、rerank_model、faq_threshold[0,1]、cluster_threshold[0,1]、gap_threshold[0,1]、min_frequency>=1、top_k[1,100]、mining_interval_seconds>=60；其他字段422。
- get_charts字段：traffic项为date/pv/uv；questions和knowledge_heat项为key/label/count；usage项为date/生成tokens/embedding_tokens/rerank_units/unknown_count；latency项为result_type/lower_ms/upper_ms|null/count；knowledge_counts为未删除/启用/已索引数量。
- search_audit项：id、actor_id、action、resource_id、at、status、request_id|null、before|null、after|null；按分项权限脱敏。

**实现状态与名称映射（2026-09-16，步骤 3 判定侧完成后）**：本节类型是设计契约（`§0.2`：签名采用跨语言记法，实现为类型化值对象），实现字段名与契约**逐字段一致**，唯一例外如下：

| 契约字段 | 实现名称 | 原因 |
|---|---|---|
| `global` | `is_global` | `global` 是 Python 关键字，无法作属性名 |

**接口字段名对应**：`PUT /api/knowledge-units/{id}/acl` 使用 `global/depts/roles/users`（API-CONTRACTS §5 API-S04、H04 `update_acl` 签名），与本节 `global/department_ids/role_ids/user_ids` 一一对应，是同一四维配置的两种记法（接口名用于传输，逻辑名用于判定类型）。

**H04 的实现分工（2026-09-16，M1-6 落地）**：`authorize_units` 的"批量读 MySQL 取当前 ACL/状态/版本"已按 §0 第 3 条的分层要求实现，**位置分工与登记符号如下（读代码时不要找错文件）**：

| 符号 | 文件 | 性质 |
|---|---|---|
| `judge`（H03）、`is_retrievable`、`filter_units`、`sanitize_denied` | `app/engines/permission.py` | 纯函数，不读库不调用模型 |
| `authorize_units(session, ctx, unit_ids) -> AuthorizeUnitsResult` | `app/services/authz_svc.py` | H04 读库与编排（session 按 §0 第 5 条注入） |
| `assemble_unit_states(units, acl) -> list[UnitState]` | 同上 | 纯函数：数据行 → 判定输入（**显式挑字段**，不夹带 `creator_id`） |
| `classify_units(ctx, states) -> AuthorizeUnitsResult` | 同上 | 纯函数：先判读权再判索引可用性，`unavailable` 与 `denied` 严格分开 |
| `AclSubjects(department_ids, role_ids, user_ids)` | 同上 | 值类型：一个单元的四维授权对象 ID |
| `AuthorizeUnitsResult(allowed, denied, unavailable, versions)` | 同上 | 值类型：H04 输出；`versions` = `unit_id → content_version` |

之所以不把读库写进 `engines/permission.py`：§0 第 3 条规定 engines 只放纯算法，把 IO 混进去会让"判定逻辑能否离线复算"变成空话。H04 的**签名与输出语义不变**，只是实现跨越了两层；上面三个纯函数之所以单独登记为符号，就是为了让最易错的分桶规则能在**没有数据库**的环境下被验证（`tests/test_authz_svc.py`，16 项）。

**`versions` 字段的解释（实现决定，需在使用处保持一致）**：返回 `dict[unit_id, content_version]`，即"放行单元应当使用的切片版本"。检索层据此取 `(unit_id, version)` 的切片——切片按版本存储（DATA-CONTRACTS §2 `chunk`），拿错版本会检索到旧内容。若检索层后续还需要 `index_generation`（用于选 collection），须先修订本节再改实现。

**本条的验证状态（2026-09-16 已实测）**：装配与分类逻辑有 16 项离线用例（`tests/test_authz_svc.py`，假行对象、不需要数据库）；**真实 MySQL 行为已通过集成验证**（`tests/integration/test_authorize_units_mysql.py`，2 项）——覆盖四维命中/未命中、墓碑、停用、索引未就绪（`pending` 与 `indexed_version < content_version`）、不存在的 ID。**PA-06 已关闭**。

## 2. 通用仓储与错误契约

- Repository.get(id:int,for_update:bool=false)->Row|null：主键读取；行锁限于短事务。
- Repository.page(filters:dict,page:int,size:int,order:list[str])->{items:list[Row],total:int}：参数绑定、排序字段白名单、权限/归属条件在分页前。
- Repository.insert(values:dict,unique_key:tuple|null)->Row：唯一约束冲突由服务决定幂等复用或409。
- Repository.update_if(id:int,expected:dict,patch:dict)->bool：单条条件UPDATE；用于revision、租约、状态比较，不匹配不覆盖。
- Repository.exists(filters:dict)->bool：部门角色ACL引用检查；删除事务与新引用竞争需要行锁/外键约束保护。
- Repository.aggregate(filters:dict,groups:list[str],measures:list[str])->list[dict]：白名单聚合；指标口径只由metrics定义。
- UnitOfWork.transaction()->AsyncContextManager：成功提交异常回滚；Row为对应实体列映射，不直接当API DTO返回。
- 普通响应{code,message,data,request_id}与正确HTTP状态；BizError含安全code/message/status，禁止原始异常直传。**每个响应都必须带 `request_id`（链路追踪 UUID），它与 `data.request_id`（问答业务的整数 ID）是两个不同的字段，命名相同但不可混用**。
- **错误码分两层，不得互相冒充**：**HTTP 层**错误码与 401/403/404/409/410/413/415/422/429/503 一一对应，经响应包返回；**任务层**错误码（解析空文/加密/损坏、向量化失败等）随 `index_task.error_code` 一类 payload 返回，此时 HTTP 仍为成功——任务是"被接受后失败"，不是"请求失败"。任务层失败不得伪装成 4xx，HTTP 层错误也不得只藏在 payload 里。

## 2.1 关键数据实体与约束

1. user/auth_session/refresh_token/role：user(username)唯一；user_role(user_id,role_id)唯一；auth_session持久存稳定登录会话；refresh_token持久存token哈希、用途、到期、撤销与消费状态。刷新消费采用行锁或条件更新；令牌原文不落库。
2. knowledge_unit：content_version、indexed_version、index_status、enabled、is_deleted、acl_version、revision；四维ACL默认全空。knowledge_version(unit_id,version)唯一；chunk(unit_id,version,seq)唯一并保留稳定chunk_id。
3. index_task：(unit_id,target_version)唯一；status、stage、attempts、next_retry_at、lease_until、lease_token。删除用cleanup_task与tombstone，不能立即删除恢复所需登记。
4. chat_request：(user_id,client_request_id)唯一；session_id、payload_hash、status、result_type、config_revision、execution_lease_until。chat_event(request_id,seq)唯一，消息来源另存message_source。
5. faq：question、answer、status、revision、cache_enabled；faq_source(faq_id,unit_id)唯一并记录内容版本。FAQ不保存可扩张的独立ACL替代来源权限。
6. mining_consumption(log_id,pipeline_version)唯一；job_key唯一保存模型起草结果。调度器max_instances只防本地重入，不能代替持久消费/租约。
7. knowledge_gap：(department_id规范化值,fingerprint)唯一；gap_request(gap_id,request_id)唯一防重计；supplement_task关联gap和后续unit，状态不混进知识索引。
8. operation_log与业务变更同事务；model_call_usage与问答按request_id关联；聚合可重算，不作为唯一原始事实。
9. 任务(status,next_retry_at)、请求(user_id,accepted_at)、消息(session_id,id)、缺口(status,last_seen_at)、FAQ来源(unit_id)建立查询索引；最终DDL需验证MySQL约束语法与执行计划。
10. **持久层实现约定（2026-09-16，M1-6 落地）**：ORM 采用 SQLAlchemy 2.0 声明式（`Mapped[]`/`mapped_column`），异步驱动 `aiomysql`；主键 BIGINT 自增；除关联表外统一 `created_at`/`updated_at`（DATETIME(6)，UTC）；可编辑实体带 `revision` 初值 1。索引与约束名由 `MetaData` 命名约定统一生成（`pk_`/`fk_`/`ix_`/`uq_`/`ck_`），不依赖数据库默认名。**DB session 按 §0.5 以请求作用域注入**（`app/db/session.py` 的 `get_session` 依赖）；事务边界由 `UnitOfWork` 承担（成功提交、异常回滚）；Row 与 API DTO 分离，**数据库行不得直接序列化给前端**。迁移用 **Alembic**（`backend/alembic/`），DDL 自 [数据契约](DATA-CONTRACTS.md) §2 生成，目标库 **MySQL 8.0.26**；禁止手写生产库、也禁止用 `metadata.create_all()` 代替迁移建库（该方法仅允许用于测试用的一次性库）。依赖声明落在 `backend/requirements.txt`——此前 backend 无任何依赖清单，属本轮补齐项。
    **配置热改参数的持久实体是 `config_revision`**（DATA-CONTRACTS §2，问答绑定其 revision）。`app/core/config.py` 旧文中"业务参数走 sys_config 表"为**笔误**——`sys_config` 在全部文档中均不存在，已按本条更正。
    **可测性边界**：实体元数据体检（命名、唯一键、可空性、公共列）不需要数据库连接；真实约束行为（唯一键竞争、FK 删除限制、行锁、utf8mb4 排序规则）必须有 MySQL 集成测试，且需先启动本机 `MySQL80` 服务，不得用 SQLite 结果替代。

## 2.2 API补充入口与薄路由契约

- GET /api/sessions → H28；GET /api/chat/requests/{request_id} → H27；GET /api/faqs → H34。
- GET /health → 进程存活与app_version；不连模型，不泄露环境变量。/ready对应F-09.04。
- 每个表单DTO字段与对应F签名一致；路径参数不在body重复定义；ctx、now、lease_token及内部任务标识由受信上下文提供。
- 删除/修改对象的expected_revision使用If-Match或body revision二选一，首版统一body；DELETE可统一If-Match，OpenAPI中明确映射至expected_revision，不同时接受冲突值。
- get_charts的range为day/week，anchor_date为ISO日期；批量接口使用multipart manifest与文件字段，不把全部内容转Base64 JSON。
- stream_events为SSE，不套普通JSON响应；GET恢复不启动生成。请求接受返回202；同键复用已有请求返回200；DTO无效422。
- **M01 实现状态（2026-09-16）**：`POST /api/auth/login`、`POST /api/auth/refresh`、`POST /api/auth/logout`、`GET /api/auth/me` 与 `GET /health` 已实现（`app/api/routes/auth.py`、`app/main.py`），端点级集成用例 16 项通过（含"注销后 access token 立即失效"）。其余入口（H27/H28/H34 等）随对应模块实现，**不得因本节存在而视为已可用**。
- **M02 实现状态（2026-09-17）**：F-02.01—F-02.09 与补充入口 API-S01/API-S02/API-S03 已实现（`app/services/org_svc.py`、`app/api/routes/org.py`、`app/schemas/org.py`）。**已知未实现（不得视为已完成）**：F-02.06 处理逻辑第 4 条"**取消相关流**"——那需要 `chat_stream`/`chat_request` 参与（M05 范围），当前只做"递增 `identity_revision` 使既有会话身份失效"；F-02.08 的"不静默撤销知识授权"仅覆盖角色被用户引用与"最后一个管理能力"两种情形，**ACL 引用检查待 M04 的 `knowledge_unit_acl` 就绪后补齐**。
- **M03 实现状态（2026-09-17 第二次更新）**：**受理与任务生命周期已实现**——F-03.01（单文件受理，幂等键→`upload_item.client_file_id`，同键同载荷 200 复用/异载荷 409）、F-03.02（批量受理，manifest 与 files 严格对齐、单项 SAVEPOINT 隔离失败）、F-03.03（进度查询，`progress` 恒 null）、F-03.05（人工重试：仅 `failed`、保留 `attempts`、revision 防并发）、F-03.06（恢复收敛：过期租约→`retry_wait`/`failed`、旧版本→`superseded`）与 **H14/H15**（租约原语，`app/tasks/lease.py`）。路由：`POST /api/uploads`、`POST /api/upload-batches`、`GET /api/index-tasks/{task_id}`、`POST /api/index-tasks/{task_id}/retry`（F-03.06 为内部调度，**无** HTTP 路由）。**仍未实现**：**F-03.04 索引流水线**（H19/H20/H21 解析清洗切片、H11 向量化、H16/H17 写入激活均未落地）——受理任务因此停留在 `queued`，`progress` 恒为 null（契约允许，AC-03.03-02）。新增两个 HTTP 409 错误码：`IDEMPOTENCY_CONFLICT`（同键不同载荷）、`TASK_STATE_CONFLICT`（任务状态不允许该操作），依据 API-CONTRACTS §1 的 409 语义。
  - **实体缺口已关闭（2026-09-17）**：开工前对账（契约声明的表集 vs `Base.metadata.tables` 做差集）发现 `DATA-CONTRACTS §2` 声明的 `upload_batch` 与 `upload_item`，在 `app/models/` 与初始迁移 `0001` 中**都不存在**——此前"M1-6 已覆盖 §2 全部实体"的说法**不成立于这两张表**。现已补齐 `app/models/ingest.py` 的 `UploadBatch`/`UploadItem` 与迁移 `0002_add_upload_batch_and_upload_item.py`；列严格取自 §2 给定字段 + §1 公共时间列（该行**未声明 `revision`**，故不引入），**未自行增删列**（§5 生成工具链）。
  - **两个唯一键是契约落点，不是可选优化**：`UNIQUE(user_id, client_batch_id)` 与 `UNIQUE(user_id, client_file_id)` 承接"网络失败后重试复用同一键、不对已接受文件创建第二个任务"（API-CONTRACTS §3）。必须由**数据库层**保证——"先查再插"在并发下会双双通过。
  - 单文件上传（`batch_id` 可空）**也要登记** `upload_item`，否则 F-03.02 的逐项结果（`unit_id|null` / `task_id|null` / `error_code|null`）无处可存，批量失败项将与客户端原清单失去对应。
  - `index_task`/`cleanup_task` 已存在，任务生命周期（F-03.03/F-03.05/F-03.06 与 H14/H15）不依赖新表。

## 2.3 三条关键调用链（有顺序约束）

1. 导入：前端H32展开→F-03.02逐项F-03.01→DB知识/版本/任务登记→H14领取→F-03.04→H19/20/21→H11→H16→H17条件激活→F-03.03查询。失败恢复F-03.06，人工重试F-03.05；删除走F-04.07/H18。
2. 问答：F-05.04创建请求/审计→F-05.05→F-06.06安全FAQ→未命中H08授权检索→全无权固定拒答；有权F-05.02/H10安全上下文→H13生成→H26先存事件→F-05.06/H07发送→F-08.01统一终态。FAQ命中也必须经过统一终态与审计，不提前yield done后才落库。
3. 沉淀：F-06.01→H22领取→H11向量化→H23聚类→H24有效来源起草→H25原子消费→H34候选列表→F-06.02编辑→F-06.03发布→F-06.06授权缓存。缺口由F-08.01触发F-07.01，F-07.03转建，补档后F-07.04原身份验证；不直接进入FAQ缓存。

## 2.4 前端状态与交互补充

**页面、路由、交互与视觉规范见 [前端规格](FRONTEND-SPEC.md)**（2026-09-16 新增）。本节只登记**函数与状态契约**；路由 path 字符串、页面布局、设计令牌属表现层约定，不在本契约层定义，避免同一事实两处漂移。跨文档引用时按 `FRONTEND-SPEC §n` 书写，`FUNCTION-MAP §2.4` 与 `FRONTEND-SPEC §n` 不同号章节是漂移高发区。

- 会话侧栏调用H28，FAQ候选/已发布切换调用H34，引用卡点击H35；未授权条目不从本地旧缓存补回。
- 上传使用XMLHttpRequest.upload.onprogress显示传输进度；任务阶段由F-03.03轮询恢复，前端不得把传输完成当索引完成。
- 消息store保存request_id、last_seq、状态、累计文本、引用与固定notice。完整应用事件成功后再提交last_seq，文本/引用/状态与游标原子更新；done/error前flush尾批；组件销毁中止订阅不自动取消服务端请求。
- 停止按钮调用F-05.07；网络重连调用F-05.06并保留after_seq；410取H27安全快照，不能重新POST同问题产生新请求。
- 权限弹窗H33处理global和多选实体；关闭global保留其他选择；全空提示无人自动可读，提交仍由F-04.08后端检查。
- 图表数据用UTC时间存储、服务端生成上海日周分桶；前端不二次按本机时区重新聚合。访问失败时不把上次缓存图冒充当前范围结果。

## 3. 功能级函数与前端调用

### M01 身份认证

- 归属：backend/app/services/auth_svc.py；前端按本模块页面/composable组织。

- **F-01.01 auth_svc.login**
  - **名称/签名**：`login(username:str,password:SecretStr,ip:str)`。
  - **职责/来源**：登录；US-01.01、AC-01.01-01/02/03。
  - **输入**：username:str,password:SecretStr,ip:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{access_token:str,refresh_token:str,expires_in:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 校验限流。
    2. 查账号验证密码及启用状态。
    3. 建立持久刷新会话。
    4. 签发两类令牌。
  - **调用方**：POST /api/auth/login。
  - **下游调用**：凭据校验或受信任务上下文 → 对应业务仓储；公开登录/刷新不调用H02，内部任务验证租约和登记来源。
  - **权限/事务**：公开；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：停用401；超限429；**密码超 72 UTF-8 字节显式拒绝、不得静默截断**（创建时长度 12—72 字节；登录允许既有短密码，但同样拒绝 >72 字节）。实现要求：长度校验在密码原语（哈希/校验函数）内完成并抛出**可识别异常**，由 API 层映射为参数错误；严禁"先截断再比对"——截断会让前 72 字节相同的两个不同密码互相通过。
  - **验证**：用AC-01.01-01构造正常数据，AC-01.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **实现状态（2026-09-16）**：服务实现 `app/services/auth_svc.py::login`，路由 `app/api/routes/auth.py::POST /api/auth/login`。限流为**单实例进程内**实现（`_LoginRateLimiter`，依据 `login_rate_per_min`）——当前目标部署是单实例 `workers=1`（DEPLOYMENT §1），故成立；**改为多实例前必须换成共享存储**，否则每个实例各算一份、上限被实例数放大。密码长度异常（`PasswordPolicyError`）映射为 422 `PASSWORD_LENGTH_INVALID`，**不退化为"用户名或密码错误"**。
  - **★ 令牌必须携带会话标识**：`auth_session` 在签发令牌前建立（处理逻辑第 3、4 步），因此 **JWT payload 增加 `sid`（auth_session_id）**，H01 才能"核对持久会话撤销"。否则 F-01.03 的"退出后该会话**两类**令牌失效"无法实现——access token 会继续有效至多 2 小时。`sid` 不是权限快照，不违反"payload 最小化"（H01 一节有说明）。
  - **前端 FE-01.01 `useauth_svc.submitLogin(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/auth/login→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-01.02 auth_svc.rotate_refresh**
  - **名称/签名**：`rotate_refresh(refresh_token:SecretStr)`。
  - **职责/来源**：刷新；US-01.02、AC-01.02-01/02/03。
  - **输入**：refresh_token:SecretStr。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{access_token:str,refresh_token:str,expires_in:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 验签与用途。
    2. 锁定持久刷新会话。
    3. 检查账号。
    4. 一次性消费旧令牌并签发新令牌。
  - **调用方**：POST /api/auth/refresh。
  - **下游调用**：刷新凭据校验 → auth_session/refresh_token仓储原子轮换。
  - **权限/事务**：有效刷新会话；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：重启后撤销仍生效；存储异常503。**撤销与会话状态必须持久化**（ARCHITECTURE §3 第 6 条；表结构见 DATA-CONTRACTS §2 `auth_session`/`refresh_token`：`token_hash` 唯一、一次性消费、`replaced_by` 记录轮换链、令牌原文不落库）。**实现状态（2026-09-16，M1-6 落地）**：DB 持久仓储已实现于 **`app/services/auth_svc.py`** 的 `PersistentRefreshStore`（表 `auth_session`/`refresh_token`，DATA-CONTRACTS §2），语义与进程内实现逐条对齐：`token_hash` 唯一保证一次性消费、`consumed_at` 记消费、`revoked_at` 记会话级撤销、`replaced_by` 记轮换链；`core/security.py` 的 `InMemoryRefreshRevocationStore` 随之降级为**仅 dev 的测试替身**（仍是 `app_env != "dev"` 构造即失败，防止误上生产）。★ 撤销"不因重启失效"这一条已由**跨进程重启验证**证明（`tests/integration/test_refresh_store_mysql.py::test_revocation_survives_process_restart`：五个独立解释器进程依次 seed→verify→revoke→verify→consume，重启后旧令牌仍被拒、且撤销后新令牌也不能消费）。**PA-07 已关闭**。
  - **验证**：用AC-01.02-01构造正常数据，AC-01.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-01.02 `useauth_svc.submitRotateRefresh(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/auth/refresh→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-01.03 auth_svc.logout**
  - **名称/签名**：`logout(ctx:UserCtx,refresh_token:SecretStr)`。
  - **职责/来源**：退出；US-01.03、AC-01.03-01/02/03。
  - **输入**：ctx:UserCtx,refresh_token:SecretStr。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{revoked:bool}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 验证归属。
    2. 撤销会话。
    3. 停止该会话活动请求。
    4. 客户端清理身份。
  - **调用方**：POST /api/auth/logout。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：登录态；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：重复退出幂等；其他设备会话不受影响。
  - **验证**：用AC-01.03-01构造正常数据，AC-01.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **实现状态（2026-09-16）**：服务实现 `app/services/auth_svc.py::logout`，路由 `app/api/routes/auth.py::POST /api/auth/logout`。幂等：重复退出返回 `{revoked:false}` 而非报错。同事务把该会话**全部未撤销的刷新令牌**一并作废（否则旧令牌仍可刷新）；access token 侧由 H01 的会话撤销核对拦住（依赖 payload 的 `sid`）。
  - **前端 FE-01.03 `useauth_svc.submitLogout(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/auth/logout→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-01.04 auth_svc.get_me**
  - **名称/签名**：`get_me(ctx:UserCtx)`。
  - **职责/来源**：当前身份；US-01.04、AC-01.04-01/02/03。
  - **输入**：ctx:UserCtx。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{user_id:int,username:str,dept_id:int|null,role_ids:list[int],permission_codes:list[str],revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 读取当前账号状态、直属部门、角色与权限码。
    2. 生成最小身份视图。
  - **调用方**：GET /api/auth/me。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：登录态；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：无部门返回null；账号停用401。
  - **验证**：用AC-01.04-01构造正常数据，AC-01.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **实现状态（2026-09-16）**：服务实现 `app/services/auth_svc.py::get_me`，路由 `app/api/routes/auth.py::GET /api/auth/me`。响应即 `app/schemas/auth.py::MeResponse`（六字段白名单），`revision` 取用户行 revision、`identity_revision` 不进入响应（它只用于服务端判断身份是否已变更）。
  - **前端 FE-01.04 `useauth_svc.submitGetMe(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/auth/me→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

### M02 组织用户与功能权限

- 归属：backend/app/services/org_svc.py；前端按本模块页面/composable组织。

- **F-02.01 org_svc.list_departments**
  - **名称/签名**：`list_departments(ctx:UserCtx)`。
  - **职责/来源**：部门树查询；US-02.01、AC-02.01-01/02/03。
  - **输入**：ctx:UserCtx。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{id:int,parent_id:int|null,name:str,revision:int}]}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 查有效部门。
    2. 构造树。
    3. 检测环及孤儿节点。
  - **调用方**：GET /api/departments。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:dept；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：异常环报配置错误，不无限递归。
  - **验证**：用AC-02.01-01构造正常数据，AC-02.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.01 `useorg_svc.submitListDepartments(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/departments→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.02 org_svc.create_department**
  - **名称/签名**：`create_department(ctx:UserCtx,parent_id:int|null,name:str)`。
  - **职责/来源**：创建部门；US-02.02、AC-02.02-01/02/03。
  - **输入**：ctx:UserCtx,parent_id:int|null,name:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{id:int,parent_id:int|null,name:str,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 查父节点。
    2. 校验同级名称。
    3. 事务创建及审计。
  - **调用方**：POST /api/departments。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:dept；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：同级重名409；无效父节点422。
  - **验证**：用AC-02.02-01构造正常数据，AC-02.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.02 `useorg_svc.submitCreateDepartment(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/departments→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.03 org_svc.update_department**
  - **名称/签名**：`update_department(ctx:UserCtx,id:int,parent_id:int|null,name:str,expected_revision:int)`。
  - **职责/来源**：编辑移动部门；US-02.03、AC-02.03-01/02/03。
  - **输入**：ctx:UserCtx,id:int,parent_id:int|null,name:str,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{id:int,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 锁定变更范围。
    2. 核对版本。
    3. 禁止移到自身后代。
    4. 更新与审计。
  - **调用方**：PATCH /api/departments/{id}。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:dept；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：版本冲突409；目标被删409。
  - **验证**：用AC-02.03-01构造正常数据，AC-02.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.03 `useorg_svc.submitUpdateDepartment(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PATCH /api/departments/{id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.04 org_svc.delete_department**
  - **名称/签名**：`delete_department(ctx:UserCtx,id:int,expected_revision:int)`。
  - **职责/来源**：删除部门；US-02.04、AC-02.04-01/02/03。
  - **输入**：ctx:UserCtx,id:int,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{deleted:bool}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 检查子部门、用户和ACL引用。
    2. 无引用删除并审计。
  - **调用方**：DELETE /api/departments/{id}。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:dept；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：不级联删除账号和授权；不存在404。
  - **验证**：用AC-02.04-01构造正常数据，AC-02.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.04 `useorg_svc.submitDeleteDepartment(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求DELETE /api/departments/{id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.05 org_svc.create_user**
  - **名称/签名**：`create_user(ctx:UserCtx,username:str,password:SecretStr,dept_id:int|null,role_ids:list[int])`。
  - **职责/来源**：创建用户；US-02.05、AC-02.05-01/02/03。
  - **输入**：ctx:UserCtx,username:str,password:SecretStr,dept_id:int|null,role_ids:list[int]。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{id:int,username:str,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 验证用户名唯一及实体有效。
    2. 安全哈希密码。
    3. 写用户与角色关系。
  - **调用方**：POST /api/users。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:user；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：未知角色422；重复账号409；不输出哈希。
  - **验证**：用AC-02.05-01构造正常数据，AC-02.05-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.05 `useorg_svc.submitCreateUser(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/users→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.06 org_svc.update_user**
  - **名称/签名**：`update_user(ctx:UserCtx,id:int,dept_id:int|null,role_ids:list[int],enabled:bool,expected_revision:int)`。
  - **职责/来源**：编辑用户启停；US-02.06、AC-02.06-01/02/03。
  - **输入**：ctx:UserCtx,id:int,dept_id:int|null,role_ids:list[int],enabled:bool,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{id:int,enabled:bool,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 锁定用户。
    2. 核对版本。
    3. 更新组织角色状态。
    4. 失效身份并取消相关流。
    5. 审计。
  - **调用方**：PATCH /api/users/{id}。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:user；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：保护最后一个系统管理账号；冲突409。
  - **验证**：用AC-02.06-01构造正常数据，AC-02.06-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.06 `useorg_svc.submitUpdateUser(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PATCH /api/users/{id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.07 org_svc.save_role**
  - **名称/签名**：`save_role(ctx:UserCtx,id:int|null,name:str,codes:list[str],expected_revision:int|null)`。
  - **职责/来源**：角色创建授权；US-02.07、AC-02.07-01/02/03。
  - **输入**：ctx:UserCtx,id:int|null,name:str,codes:list[str],expected_revision:int|null。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{id:int,codes:list[str],revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 只接受14个功能码。
    2. 创建或条件更新角色。
    3. 替换关联。
    4. 失效成员身份。
  - **调用方**：PUT /api/roles。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:role；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：未知码422；角色不带正文旁路。
  - **验证**：用AC-02.07-01构造正常数据，AC-02.07-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.07 `useorg_svc.submitSaveRole(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PUT /api/roles→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.08 org_svc.delete_role**
  - **名称/签名**：`delete_role(ctx:UserCtx,id:int,expected_revision:int)`。
  - **职责/来源**：删除角色；US-02.08、AC-02.08-01/02/03。
  - **输入**：ctx:UserCtx,id:int,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{deleted:bool}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核对版本及用户/ACL引用。
    2. 无引用删除。
    3. 审计。
  - **调用方**：DELETE /api/roles/{id}。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:role；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：不静默撤销知识授权；保留最后一个管理能力。
  - **验证**：用AC-02.08-01构造正常数据，AC-02.08-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.08 `useorg_svc.submitDeleteRole(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求DELETE /api/roles/{id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-02.09 org_svc.list_directory**
  - **名称/签名**：`list_directory(ctx:UserCtx,kind:str,q:str,page:int,size:int)`。
  - **职责/来源**：用户角色分页查询；US-02.09、AC-02.09-01/02/03。
  - **输入**：ctx:UserCtx,kind:str,q:str,page:int,size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{id:int,label:str,enabled:bool|null}],total:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 按kind检查sys:user或sys:role。
    2. 参数化筛选。
    3. 稳定分页。
  - **调用方**：GET /api/directory。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：按kind检查；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：size1–100；不输出密码令牌。
  - **验证**：用AC-02.09-01构造正常数据，AC-02.09-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-02.09 `useorg_svc.submitListDirectory(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/directory→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **API-S01 org_svc.list_users（M02 补充入口；API-CONTRACTS §4 `API-S01`）**
  - **名称/签名**：`list_users(session:AsyncSession,ctx:UserCtx,*,q:str="",page:int=1,size:int=20,enabled:bool|None=None)`；返回 `(items,total)`。
  - **职责/来源**：用户分页列表；AC-02.09-01（"无重复漏页；账号可按状态查询"）。
  - **输出**：`items:[{id:int,username:str,dept_id:int|null,role_ids:list[int],enabled:bool,revision:int}]`、`total:int`。**不返回 `password_hash`**（AC-02.05-02"不输出哈希"）。
  - **处理逻辑**：① 按 `enabled` 与 `q` 组装条件（`q` 同时匹配 `username` 原样与 `username_norm` 归一化值）；② **先计数后分页**（权限过滤在计数之前，FUNCTION-MAP §2）；③ 稳定排序 `created_at DESC,id DESC`；④ 角色 ID **批量一次 `IN` 查询**后再分组（避免每行一次往返的 N+1）。
  - **调用方**：GET /api/users。
  - **权限**：`sys:user`（由 H02 依赖施加；本入口无行级归属过滤，因为该权限本身即管理面）。
  - **边界及错误**：`page<1` 归一到 1；`size` 夹取到 `1..page_size_max`（部署配置，默认 100）。
  - **登记理由**：该符号原为"路由直接拼查询"而漏登；按流程规则"契约层公开符号必须先登记"，此处补齐（门禁第 8 项据此拦截）。

- **API-S02 org_svc.list_roles（M02 补充入口；API-CONTRACTS §4 `API-S02`）**
  - **名称/签名**：`list_roles(session:AsyncSession,ctx:UserCtx,*,q:str="",page:int=1,size:int=20)`；返回 `(items,total)`。
  - **职责/来源**：角色分页列表；AC-02.09-01。
  - **输出**：`items:[{id:int,name:str,codes:list[str],revision:int}]`、`total:int`；`codes` 升序排列（前端权限树据此回填）。
  - **处理逻辑**：① 按 `q` 过滤；② 计数 → 稳定排序分页；③ 角色权限码**批量一次 `IN` 查询**后分组。
  - **调用方**：GET /api/roles。
  - **权限**：`sys:role`。
  - **边界及错误**：同 API-S01 的分页夹取规则。

- **API-S03 org_svc.list_permission_codes（M02 补充入口；API-CONTRACTS §4 `API-S03`）**
  - **名称/签名**：`list_permission_codes(session:AsyncSession,ctx:UserCtx) -> list[{code:str,label:str,module:str}]`。
  - **职责/来源**：权限码字典；AC-02.07-01（"前端菜单与后端授权一致"）。
  - **输出**：**固定 14 项**，顺序取自 `app/core/permissions.py::PERMISSIONS`。
  - **处理逻辑**：直接由 `PERMISSIONS` 构造，**不从数据库读**——权限码的唯一来源是代码里的列表，从库里读会允许"库里多一个码而前端不认"的漂移。
  - **调用方**：GET /api/permission-codes。
  - **权限**：`sys:role`。
  - **边界及错误**：无分页参数；无输入。

### M03 文档导入解析与任务

- 归属：backend/app/services/ingest_svc.py；前端按本模块页面/composable组织。

- **F-03.01 ingest_svc.accept_upload**
  - **名称/签名**：`accept_upload(ctx:UserCtx,file:UploadFile,category:str,client_upload_id:UUID)`。
  - **职责/来源**：单文件上传；US-03.01、AC-03.01-01/02/03。
  - **输入**：ctx:UserCtx,file:UploadFile,category:str,client_upload_id:UUID。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{unit_id:int,task_id:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 校验扩展名内容与20MiB上限。
    2. UUID暂存并算哈希。
    3. 事务登记单元版本任务。
    4. 失败回收临时文件。
  - **调用方**：POST /api/uploads。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：kb:upload；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：415不支持、413超限；不跨权限泄露重复文件。
  - **验证**：用AC-03.01-01构造正常数据，AC-03.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-03.01 `useingest_svc.submitAcceptUpload(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/uploads→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-03.02 ingest_svc.accept_batch**
  - **名称/签名**：`accept_batch(ctx:UserCtx,files:list[UploadFile],items:list[{client_file_id:UUID,relative_path:str,category:str}],client_batch_id:UUID)`。
  - **职责/来源**：批量文件夹导入；US-03.02、AC-03.02-01/02/03。
  - **输入**：ctx:UserCtx,files:list[UploadFile],items:list[{client_file_id:UUID,relative_path:str,category:str}],client_batch_id:UUID。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{batch_id:int,items:list[{client_file_id:UUID,relative_path:str,unit_id:int|null,task_id:int|null,error_code:str|null}]}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 浏览器展开目录。
    2. 相对路径只展示。
    3. 逐文件调用accept_upload。
    4. 独立汇总。
  - **调用方**：POST /api/upload-batches。
  - **下游调用**：H32（前端）→F-03.01（逐项）。
  - **权限/事务**：kb:upload；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：暂定100文件/200MiB/并发3，可配置；禁止路径穿越。
  - **验证**：用AC-03.02-01构造正常数据，AC-03.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-03.02 `useingest_svc.submitAcceptBatch(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/upload-batches→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-03.03 ingest_svc.get_task**
  - **名称/签名**：`get_task(ctx:UserCtx,task_id:int)`。
  - **职责/来源**：进度查询恢复；US-03.03、AC-03.03-01/02/03。
  - **输入**：ctx:UserCtx,task_id:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{status:str,stage:str,progress:int|null,error_code:str|null,attempts:int,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核验任务归属或管理权限。
    2. 读取持久状态。
  - **调用方**：GET /api/index-tasks/{task_id}。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：kb:upload或kb:view；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：未知进度显示进行中；不存在404。
  - **验证**：用AC-03.03-01构造正常数据，AC-03.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-03.03 `useingest_svc.submitGetTask(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/index-tasks/{task_id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-03.04 ingest_svc.run_pipeline**
  - **名称/签名**：`run_pipeline(task_id:int,lease_token:UUID)`。
  - **职责/来源**：执行索引任务；US-03.04、AC-03.04-01/02/03。
  - **输入**：task_id:int,lease_token:UUID。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{status:str,indexed_version:int|null,error_code:str|null}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核验租约。
    2. 解析清洗切片。
    3. 持久版本片段。
    4. 批量嵌入。
    5. 写版本向量。
    6. 校验后条件激活。
  - **调用方**：内部任务。
  - **下游调用**：H15→H19→H20→H21→H11→H16→H17。
  - **权限/事务**：内部执行器；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：旧任务superseded；永久错误failed；瞬时错误retry_wait。
  - **验证**：用AC-03.04-01构造正常数据，AC-03.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-03.05 ingest_svc.retry_task**
  - **名称/签名**：`retry_task(ctx:UserCtx,task_id:int,expected_revision:int)`。
  - **职责/来源**：人工重试；US-03.05、AC-03.05-01/02/03。
  - **输入**：ctx:UserCtx,task_id:int,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{task_id:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 只允许失败当前版本。
    2. 重置queued保留尝试历史。
    3. 审计。
  - **调用方**：POST /api/index-tasks/{task_id}/retry。
  - **下游调用**：H02→任务条件更新。
  - **权限/事务**：kb:edit；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：运行中409；已删或被替代任务409。
  - **验证**：用AC-03.05-01构造正常数据，AC-03.05-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-03.05 `useingest_svc.submitRetryTask(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/index-tasks/{task_id}/retry→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-03.06 ingest_svc.recover_tasks**
  - **名称/签名**：`recover_tasks(now:datetime,batch_size:int)`。
  - **职责/来源**：任务恢复补偿；US-03.06、AC-03.06-01/02/03。
  - **输入**：now:datetime,batch_size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{requeued:int,superseded:int,failed:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 扫描过期租约和到期重试。
    2. 条件领取。
    3. 旧版本作废。
    4. 当前版本重排执行。
  - **调用方**：内部调度。
  - **下游调用**：任务扫描→H14→F-03.04。
  - **权限/事务**：内部执行器；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：临时失败最多5次；永久错误不循环。
  - **验证**：用AC-03.06-01构造正常数据，AC-03.06-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

### M04 知识生命周期与四维权限

- 归属：backend/app/services/knowledge_svc.py；前端按本模块页面/composable组织。

- **F-04.01 knowledge_svc.list_units**
  - **名称/签名**：`list_units(ctx:UserCtx,q:str,category:str|null,enabled:bool|null,page:int,size:int)`。
  - **职责/来源**：台账与卡片查询；US-04.01、AC-04.01-01/02/03。
  - **输入**：ctx:UserCtx,q:str,category:str|null,enabled:bool|null,page:int,size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{id:int,code:str,title:str,format:str,category:str,acl_tags:list[str],updated_at:datetime,enabled:bool,index_status:str,revision:int}],total:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 检查kb:view。
    2. 未删除台账筛选。
    3. 稳定分页。
    4. 不查正文。
  - **调用方**：GET /api/knowledge-units。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：kb:view；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：kb:view是管理元数据显式能力，不能扩展为正文读权。
  - **验证**：用AC-04.01-01构造正常数据，AC-04.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.01 `useknowledge_svc.submitListUnits(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/knowledge-units→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.02 knowledge_svc.update_metadata**
  - **名称/签名**：`update_metadata(ctx:UserCtx,unit_id:int,title:str,category:str,expected_revision:int)`。
  - **职责/来源**：修改标题分类；US-04.02、AC-04.02-01/02/03。
  - **输入**：ctx:UserCtx,unit_id:int,title:str,category:str,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{unit_id:int,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核验存在与版本。
    2. 更新标题分类。
    3. 审计。
  - **调用方**：PATCH /api/knowledge-units/{unit_id}。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：kb:edit；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：空标题422；冲突409。
  - **验证**：用AC-04.02-01构造正常数据，AC-04.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.02 `useknowledge_svc.submitUpdateMetadata(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PATCH /api/knowledge-units/{unit_id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.03 knowledge_svc.read_chunks**
  - **名称/签名**：`read_chunks(ctx:UserCtx,unit_id:int,version:int|null,page:int,size:int)`。
  - **职责/来源**：查看正文切片；US-04.03、AC-04.03-01/02/03。
  - **输入**：ctx:UserCtx,unit_id:int,version:int|null,page:int,size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{chunk_id:int,version:int,seq:int,text:str,page_no:int|null,offset:int}],total:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 检查功能及数据读权。
    2. 查有效版本片段。
    3. 附原文定位。
  - **调用方**：GET /api/knowledge-units/{unit_id}/chunks。
  - **下游调用**：H02→H04→版本切片仓储。
  - **权限/事务**：kb:view+数据读权；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：停用只可管理台账；无权响应不含正文。
  - **验证**：用AC-04.03-01构造正常数据，AC-04.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.03 `useknowledge_svc.submitReadChunks(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/knowledge-units/{unit_id}/chunks→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.04 knowledge_svc.replace_document**
  - **名称/签名**：`replace_document(ctx:UserCtx,unit_id:int,file:UploadFile,expected_revision:int)`。
  - **职责/来源**：替换文档；US-04.04、AC-04.04-01/02/03。
  - **输入**：ctx:UserCtx,unit_id:int,file:UploadFile,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{task_id:int,target_version:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 校验读权和文件。
    2. 增内容版本。
    3. 保留ACL。
    4. 事务stale与任务。
    5. 失效FAQ。
  - **调用方**：POST /api/knowledge-units/{unit_id}/versions。
  - **下游调用**：H02→H04→版本/任务仓储→F-06.07。
  - **权限/事务**：kb:edit+数据读权；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：旧任务不能覆盖；失败保留记录供恢复。
  - **验证**：用AC-04.04-01构造正常数据，AC-04.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.04 `useknowledge_svc.submitReplaceDocument(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/knowledge-units/{unit_id}/versions→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.05 knowledge_svc.mutate_chunks**
  - **名称/签名**：`mutate_chunks(ctx:UserCtx,unit_id:int,chunk_id:int,action:str,text:str|null,split_offset:int|null,expected_revision:int)`。
  - **职责/来源**：切片编辑拆分删除；US-04.05、AC-04.05-01/02/03。
  - **输入**：ctx:UserCtx,unit_id:int,chunk_id:int,action:str,text:str|null,split_offset:int|null,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{task_id:int,target_version:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 授权。
    2. 创建新版本。
    3. 按稳定chunk_id修改。
    4. 在新版本生成seq。
    5. 登记索引任务。
  - **调用方**：POST /api/knowledge-units/{unit_id}/chunk-mutations。
  - **下游调用**：H02→H04→版本/切片/任务仓储→F-06.07。
  - **权限/事务**：kb:edit+数据读权；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：空拆分或越界422；不得就地重排旧版本序号。
  - **验证**：用AC-04.05-01构造正常数据，AC-04.05-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.05 `useknowledge_svc.submitMutateChunks(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/knowledge-units/{unit_id}/chunk-mutations→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.06 knowledge_svc.set_enabled**
  - **名称/签名**：`set_enabled(ctx:UserCtx,unit_id:int,enabled:bool,expected_revision:int)`。
  - **职责/来源**：知识启停；US-04.06、AC-04.06-01/02/03。
  - **输入**：ctx:UserCtx,unit_id:int,enabled:bool,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{unit_id:int,enabled:bool,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 条件更新。
    2. 失效FAQ与相关活动输出。
    3. 审计。
  - **调用方**：PUT /api/knowledge-units/{unit_id}/enabled。
  - **下游调用**：H02→单元条件更新→F-06.07。
  - **权限/事务**：kb:edit；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：重新启用仍须有效索引与ACL；删除不可启用。
  - **验证**：用AC-04.06-01构造正常数据，AC-04.06-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.06 `useknowledge_svc.submitSetEnabled(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PUT /api/knowledge-units/{unit_id}/enabled→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.07 knowledge_svc.delete_unit**
  - **名称/签名**：`delete_unit(ctx:UserCtx,unit_id:int,expected_revision:int)`。
  - **职责/来源**：知识删除；US-04.07、AC-04.07-01/02/03。
  - **输入**：ctx:UserCtx,unit_id:int,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{deletion_id:int,cleanup_status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 事务墓碑与清理任务。
    2. 失效派生内容。
    3. 异步清理文件向量。
  - **调用方**：DELETE /api/knowledge-units/{unit_id}。
  - **下游调用**：H02→墓碑/清理任务仓储→F-06.07→H18。
  - **权限/事务**：kb:delete；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：重复返回既有结果；任务不得复活单元。
  - **验证**：用AC-04.07-01构造正常数据，AC-04.07-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.07 `useknowledge_svc.submitDeleteUnit(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求DELETE /api/knowledge-units/{unit_id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.08 knowledge_svc.update_acl**
  - **名称/签名**：`update_acl(ctx:UserCtx,unit_id:int,global:bool,depts:list[int],roles:list[int],users:list[int],expected_revision:int)`。
  - **职责/来源**：四维配置；US-04.08、AC-04.08-01/02/03。
  - **输入**：ctx:UserCtx,unit_id:int,global:bool,depts:list[int],roles:list[int],users:list[int],expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{acl_version:int,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 检查kb:perm。
    2. 验证实体。
    3. 条件替换ACL。
    4. 增加版本。
    5. 失效缓存流。
    6. before/after审计。
  - **调用方**：PUT /api/knowledge-units/{unit_id}/acl。
  - **下游调用**：H02→ACL实体检查→条件更新仓储→F-06.07→活动流失效。
  - **权限/事务**：kb:perm；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：kb:perm可显式授权自己但必须审计；无效实体422。
  - **验证**：用AC-04.08-01构造正常数据，AC-04.08-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.08 `useknowledge_svc.submitUpdateAcl(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PUT /api/knowledge-units/{unit_id}/acl→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-04.09 knowledge_svc.list_acl_entities**
  - **名称/签名**：`list_acl_entities(ctx:UserCtx,kind:str,q:str,page:int,size:int)`。
  - **职责/来源**：权限实体选择；US-04.09、AC-04.09-01/02/03。
  - **输入**：ctx:UserCtx,kind:str,q:str,page:int,size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{id:int,label:str,parent_id:int|null}],total:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 授权。
    2. 按部门角色人员查询最小字段。
    3. 人员分页。
  - **调用方**：GET /api/acl-entities。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：kb:perm；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：不返回非必要个人资料；已删除实体不可选。
  - **验证**：用AC-04.09-01构造正常数据，AC-04.09-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-04.09 `useknowledge_svc.submitListAclEntities(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/acl-entities→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

### M05 会话检索与流式问答

- 归属：backend/app/services/chat_svc.py；前端按本模块页面/composable组织。

- **F-05.01 chat_svc.create_session**
  - **名称/签名**：`create_session(ctx:UserCtx,title:str|null)`。
  - **职责/来源**：创建会话；US-05.01、AC-05.01-01/02/03。
  - **输入**：ctx:UserCtx,title:str|null。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{session_id:int,title:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 授权。
    2. 创建当前用户拥有的会话。
  - **调用方**：POST /api/sessions。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：ai:ask；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：所有者不接受客户端指定；标题最多100字。
  - **验证**：用AC-05.01-01构造正常数据，AC-05.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.01 `usechat_svc.submitCreateSession(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/sessions→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-05.02 chat_svc.read_history**
  - **名称/签名**：`read_history(ctx:UserCtx,session_id:int,page:int,size:int)`。
  - **职责/来源**：历史查询；US-05.02、AC-05.02-01/02/03。
  - **输入**：ctx:UserCtx,session_id:int,page:int,size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{id:int,role:str,text:str|null,restricted:bool}],total:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核验归属。
    2. 查消息及来源。
    3. 每份派生答案全部来源重新授权。
    4. 受限整段占位。
  - **调用方**：GET /api/sessions/{session_id}/messages。
  - **下游调用**：会话归属仓储→H05→消息仓储。
  - **权限/事务**：ai:ask；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：他人会话404；无来源旧知识答案不回填。
  - **验证**：用AC-05.02-01构造正常数据，AC-05.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.02 `usechat_svc.submitReadHistory(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/sessions/{session_id}/messages→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-05.03 chat_svc.mutate_session**
  - **名称/签名**：`mutate_session(ctx:UserCtx,session_id:int,action:str,title:str|null)`。
  - **职责/来源**：会话重命名删除；US-05.03、AC-05.03-01/02/03。
  - **输入**：ctx:UserCtx,session_id:int,action:str,title:str|null。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{session_id:int,deleted:bool}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核验归属。
    2. 重命名或逻辑删除。
    3. 删除时取消运行请求。
    4. 保留审计。
  - **调用方**：PATCH /api/sessions/{session_id}。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：ai:ask；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：重复删除幂等；不把删除会话当清除审计。
  - **验证**：用AC-05.03-01构造正常数据，AC-05.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.03 `usechat_svc.submitMutateSession(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PATCH /api/sessions/{session_id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-05.04 chat_svc.accept_question**
  - **名称/签名**：`accept_question(ctx:UserCtx,session_id:int,client_request_id:UUID,question:str)`。
  - **职责/来源**：幂等提交问题；US-05.04、AC-05.04-01/02/03。
  - **输入**：ctx:UserCtx,session_id:int,client_request_id:UUID,question:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{request_id:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 验证归属及长度。
    2. 唯一键创建请求、初始审计和任务。
    3. 同键同载荷复用。
  - **调用方**：POST /api/chat/requests。
  - **下游调用**：H02→归属校验→请求幂等仓储→持久任务登记。
  - **权限/事务**：ai:ask；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：同键异载荷409；问题1–8000字；并发上限429。
  - **验证**：用AC-05.04-01构造正常数据，AC-05.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.04 `usechat_svc.submitAcceptQuestion(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/chat/requests→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-05.05 chat_svc.run_answer**
  - **名称/签名**：`run_answer(request_id:int)`。
  - **职责/来源**：执行授权回答；US-05.05、AC-05.05-01/02/03。
  - **输入**：request_id:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{status:str,result_type:str,error_code:str|null}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 加载当前身份。
    2. 授权FAQ匹配。
    3. 否则授权检索。
    4. 分类结果。
    5. 构造安全历史Prompt。
    6. 生成并持久事件。
    7. 统一终态。
  - **调用方**：内部任务。
  - **下游调用**：H01→F-06.06→H08→F-05.02→H10→H13→H26→F-08.01。
  - **权限/事务**：内部执行器；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：异常不冒充无知识；撤权取消终止；未知用量不填0。
  - **验证**：用AC-05.05-01构造正常数据，AC-05.05-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-05.06 chat_svc.stream_events**
  - **名称/签名**：`stream_events(ctx:UserCtx,request_id:int,after_seq:int)`。
  - **职责/来源**：事件订阅恢复；US-05.06、AC-05.06-01/02/03。
  - **输入**：ctx:UserCtx,request_id:int,after_seq:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{AsyncIterator[SseEvent]}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 归属及来源再授权。
    2. 读取seq后事件。
    3. 附着同请求。
    4. 15秒心跳。
    5. 发送批次前授权。
  - **调用方**：GET /api/chat/requests/{request_id}/events。
  - **下游调用**：H27→H05→H07；前端H31。
  - **权限/事务**：ai:ask；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：保留期外410转快照；撤权后不重放原文。
  - **验证**：用AC-05.06-01构造正常数据，AC-05.06-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.06 `usechat_svc.submitStreamEvents(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/chat/requests/{request_id}/events→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-05.07 chat_svc.cancel_request**
  - **名称/签名**：`cancel_request(ctx:UserCtx,request_id:int)`。
  - **职责/来源**：取消请求；US-05.07、AC-05.07-01/02/03。
  - **输入**：ctx:UserCtx,request_id:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{request_id:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核验归属。
    2. 原子取消标记。
    3. 停止生成。
    4. 终态持久化。
  - **调用方**：POST /api/chat/requests/{request_id}/cancel。
  - **下游调用**：H27→请求取消标记→CancelSignal→F-08.01。
  - **权限/事务**：ai:ask；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：完成态不改为取消；断网不等于主动取消。
  - **验证**：用AC-05.07-01构造正常数据，AC-05.07-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.07 `usechat_svc.submitCancelRequest(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/chat/requests/{request_id}/cancel→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-05.08 chat_svc.read_citation**
  - **名称/签名**：`read_citation(ctx:UserCtx,request_id:int,no:int)`。
  - **职责/来源**：引用详情定位；US-05.08、AC-05.08-01/02/03。
  - **输入**：ctx:UserCtx,request_id:int,no:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{no:int,unit_id:int,version:int,chunk_id:int,title:str,snippet:str,page_no:int|null,offset:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 核验请求归属。
    2. 按已记录来源版本。
    3. 当前授权。
    4. 返回原文定位。
  - **调用方**：GET /api/chat/requests/{request_id}/citations/{no}。
  - **下游调用**：H27→H05→版本切片仓储。
  - **权限/事务**：ai:ask+数据读权；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：旧版本不可用受控提示；无权不返回标题正文。
  - **验证**：用AC-05.08-01构造正常数据，AC-05.08-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.08 `usechat_svc.submitReadCitation(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/chat/requests/{request_id}/citations/{no}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-05.09 chat_svc.suggest**
  - **名称/签名**：`suggest(ctx:UserCtx,prefix:str,limit:int)`。
  - **职责/来源**：智能联想；US-05.09、AC-05.09-01/02/03。
  - **输入**：ctx:UserCtx,prefix:str,limit:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[str]}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 可读FAQ和知识标题、本人历史取候选。
    2. 全部授权。
    3. 前缀匹配去重。
  - **调用方**：GET /api/chat/suggestions。
  - **下游调用**：候选查询→H04/H05→前缀去重。
  - **权限/事务**：ai:ask；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：limit1–10；空输入空结果；前端防抖。
  - **验证**：用AC-05.09-01构造正常数据，AC-05.09-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-05.09 `usechat_svc.submitSuggest(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/chat/suggestions→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

### M06 FAQ沉淀审核与缓存

- 归属：backend/app/services/faq_svc.py；前端按本模块页面/composable组织。

- **F-06.01 faq_svc.run_mining**
  - **名称/签名**：`run_mining(run_id:int,trigger:str,pipeline_version:str)`。
  - **职责/来源**：运行增量挖掘；US-06.01、AC-06.01-01/02/03。
  - **输入**：run_id:int,trigger:str,pipeline_version:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{run_id:int,consumed:int,candidates:int,failed:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 持久领取未消费终态日志。
    2. 分组向量聚类。
    3. 有效来源起草。
    4. 消费与候选同事务提交。
  - **调用方**：受信后台任务；POST /api/mining/runs 的薄适配器先鉴权登记，再异步调用。
  - **下游调用**：H22→H11→H23→H24→H25。
  - **权限/事务**：faq:review或内部调度；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：FAQ命中不反馈循环；模型调用不占长事务。
  - **验证**：用AC-06.01-01构造正常数据，AC-06.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-06.01 `usefaq_svc.submitRunMining(form: {client_action_id:UUID,pipeline_version:str}) -> Promise<{run_id:int,status:str}>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/mining/runs→取得run_id并轮询GET /api/mining/runs/{id}→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-06.02 faq_svc.edit_candidate**
  - **名称/签名**：`edit_candidate(ctx:UserCtx,faq_id:int,question:str,answer:str,source_ids:list[int],expected_revision:int)`。
  - **职责/来源**：候选编辑；US-06.02、AC-06.02-01/02/03。
  - **输入**：ctx:UserCtx,faq_id:int,question:str,answer:str,source_ids:list[int],expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{faq_id:int,revision:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 检查审核权限及全部来源读权。
    2. 非空来源。
    3. 版本编辑及审计。
  - **调用方**：PATCH /api/faqs/{faq_id}/candidate。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：faq:review+全部来源读权；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：已发布先下线再编辑；来源无权403。
  - **验证**：用AC-06.02-01构造正常数据，AC-06.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-06.02 `usefaq_svc.submitEditCandidate(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PATCH /api/faqs/{faq_id}/candidate→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-06.03 faq_svc.publish**
  - **名称/签名**：`publish(ctx:UserCtx,faq_id:int,expected_revision:int)`。
  - **职责/来源**：发布FAQ；US-06.03、AC-06.03-01/02/03。
  - **输入**：ctx:UserCtx,faq_id:int,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{faq_id:int,status:str,revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 验证全部来源当前版本权限。
    2. 禁止扩展来源ACL。
    3. 事务发布审计。
    4. 版本化缓存。
  - **调用方**：POST /api/faqs/{faq_id}/publish。
  - **下游调用**：H02→H05→FAQ条件更新仓储→缓存。
  - **权限/事务**：faq:publish+全部来源读权；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：来源为空删除或变更409；禁止extra_perms放宽。
  - **验证**：用AC-06.03-01构造正常数据，AC-06.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-06.03 `usefaq_svc.submitPublish(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/faqs/{faq_id}/publish→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-06.04 faq_svc.change_status**
  - **名称/签名**：`change_status(ctx:UserCtx,faq_id:int,action:str,reason:str,expected_revision:int)`。
  - **职责/来源**：驳回下线；US-06.04、AC-06.04-01/02/03。
  - **输入**：ctx:UserCtx,faq_id:int,action:str,reason:str,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{faq_id:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. reject仅candidate。
    2. offline仅published；resubmit仅rejected/offline/stale，复核来源后回candidate。
    3. 对应功能鉴权。
    4. 状态更新审计清缓存。
  - **调用方**：POST /api/faqs/{faq_id}/status。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：faq:review或faq:publish；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：理由必填；驳回不自动创造缺口。
  - **验证**：用AC-06.04-01构造正常数据，AC-06.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-06.04 `usefaq_svc.submitChangeStatus(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/faqs/{faq_id}/status→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-06.05 faq_svc.set_cache_enabled**
  - **名称/签名**：`set_cache_enabled(ctx:UserCtx,faq_id:int,enabled:bool,expected_revision:int)`。
  - **职责/来源**：缓存启停；US-06.05、AC-06.05-01/02/03。
  - **输入**：ctx:UserCtx,faq_id:int,enabled:bool,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{faq_id:int,enabled:bool,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 校验发布功能。
    2. 更新开关并失效缓存，审核状态独立。
  - **调用方**：PUT /api/faqs/{faq_id}/cache-enabled。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：faq:publish；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：stale不能靠开关恢复直出。
  - **验证**：用AC-06.05-01构造正常数据，AC-06.05-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-06.05 `usefaq_svc.submitSetCacheEnabled(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PUT /api/faqs/{faq_id}/cache-enabled→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-06.06 faq_svc.match_authorized**
  - **名称/签名**：`match_authorized(ctx:UserCtx,question:str)`。
  - **职责/来源**：授权缓存匹配；US-06.06、AC-06.06-01/02/03。
  - **输入**：ctx:UserCtx,question:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{hit:bool,faq_id:int|null,answer:str|null,sources:list[SourceRef],usage:Usage}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 精确规范化匹配优先。
    2. 其次语义候选。
    3. 逐项当前来源授权与状态复核。
    4. 返回首个合格命中。
  - **调用方**：内部调用。
  - **下游调用**：精确匹配→必要时H11→H05→FAQ状态仓储。
  - **权限/事务**：内部；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：高分无权跳过；鉴权异常503，不能当普通未命中。
  - **验证**：用AC-06.06-01构造正常数据，AC-06.06-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-06.07 faq_svc.invalidate_sources**
  - **名称/签名**：`invalidate_sources(unit_id:int,change:str)`。
  - **职责/来源**：来源变化失效；US-06.07、AC-06.07-01/02/03。
  - **输入**：unit_id:int,change:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{staled:int,evicted:int,cancelled:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 反查faq_source。
    2. 正文或删除转stale。
    3. ACL启停强制复核。
    4. 取消相关输出。
  - **调用方**：内部调用。
  - **下游调用**：凭据校验或受信任务上下文 → 对应业务仓储；公开登录/刷新不调用H02，内部任务验证租约和登记来源。
  - **权限/事务**：内部；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：缓存失败不影响DB权威拒绝。
  - **验证**：用AC-06.07-01构造正常数据，AC-06.07-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

### M07 知识缺口闭环

- 归属：backend/app/services/gap_svc.py；前端按本模块页面/composable组织。

- **F-07.01 gap_svc.record_gap**
  - **名称/签名**：`record_gap(request_id:int,result_type:str,question:str,dept_id:int|null,score:float|null,score_type:str,model_version:str)`。
  - **职责/来源**：缺口登记；US-07.01、AC-07.01-01/02/03。
  - **输入**：request_id:int,result_type:str,question:str,dept_id:int|null,score:float|null,score_type:str,model_version:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{gap_id:int|null}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 仅正常no_evidence/low_confidence。
    2. 按部门指纹聚合。
    3. request_id去重。
    4. 记录分数语义。
  - **调用方**：内部调用。
  - **下游调用**：凭据校验或受信任务上下文 → 对应业务仓储；公开登录/刷新不调用H02，内部任务验证租约和登记来源。
  - **权限/事务**：内部；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：RRF不当置信度；未知状态拒绝入池。
  - **验证**：用AC-07.01-01构造正常数据，AC-07.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-07.02 gap_svc.list_gaps**
  - **名称/签名**：`list_gaps(ctx:UserCtx,status:str|null,dept_id:int|null,page:int,size:int)`。
  - **职责/来源**：缺口查询；US-07.02、AC-07.02-01/02/03。
  - **输入**：ctx:UserCtx,status:str|null,dept_id:int|null,page:int,size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{id:int,question:str,dept_id:int|null,recent_frequency:int,max_similarity:float|null,suggested_category:str,last_seen_at:datetime,status:str}],total:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 检查gap:handle。
    2. 按状态部门时间筛选分页。
    3. 受控运营查询审计。
  - **调用方**：GET /api/knowledge-gaps。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：gap:handle；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：近期频次按窗口，不以全量累计冒充；不附受限答案。
  - **验证**：用AC-07.02-01构造正常数据，AC-07.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-07.02 `usegap_svc.submitListGaps(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/knowledge-gaps→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-07.03 gap_svc.convert_gap**
  - **名称/签名**：`convert_gap(ctx:UserCtx,gap_id:int,client_action_id:UUID)`。
  - **职责/来源**：转建补充任务；US-07.03、AC-07.03-01/02/03。
  - **输入**：ctx:UserCtx,gap_id:int,client_action_id:UUID。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{task_id:int,gap_id:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 锁定缺口。
    2. 幂等创建supplement_task。
    3. gap进入processing。
    4. 随后绑定上传资料。
  - **调用方**：POST /api/knowledge-gaps/{gap_id}/convert。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：gap:handle；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：processing不写knowledge.index_status；上传另需kb:upload。
  - **验证**：用AC-07.03-01构造正常数据，AC-07.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-07.03 `usegap_svc.submitConvertGap(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/knowledge-gaps/{gap_id}/convert→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-07.04 gap_svc.verify_gap**
  - **名称/签名**：`verify_gap(ctx:UserCtx,gap_id:int)`。
  - **职责/来源**：回放关闭；US-07.04、AC-07.04-01/02/03。
  - **输入**：ctx:UserCtx,gap_id:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{passed:bool,state:str,reason:str|null}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 用原问题当前合法身份上下文检索。
    2. 检查命中关联补充版本且质量达标。
    3. 条件关闭。
  - **调用方**：POST /api/knowledge-gaps/{gap_id}/verify。
  - **下游调用**：原合法身份上下文→H08→gap条件关闭。
  - **权限/事务**：gap:handle；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：原用户停用blocked；失败保持processing。
  - **验证**：用AC-07.04-01构造正常数据，AC-07.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-07.04 `usegap_svc.submitVerifyGap(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/knowledge-gaps/{gap_id}/verify→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

### M08 审计与数据看板

- 归属：backend/app/services/metrics_svc.py；前端按本模块页面/composable组织。

- **F-08.01 metrics_svc.finalize_request**
  - **名称/签名**：`finalize_request(request_id:int,result:AnswerResult,usage:Usage)`。
  - **职责/来源**：请求终态审计；US-08.01、AC-08.01-01/02/03。
  - **输入**：request_id:int,result:AnswerResult,usage:Usage。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{request_id:int,status:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 条件更新唯一终态。
    2. 写来源/允许拒绝列表/耗时/用量。
    3. 同事务写入最终done/error事件，提交后发送。
    4. 同事务登记缺口outbox，消费者按request_id幂等处理。
  - **调用方**：内部调用。
  - **下游调用**：请求终态仓储→用量/审计仓储→H26→F-07.01。
  - **权限/事务**：内部；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：遗留running恢复为failed；未知用量不伪0。
  - **验证**：用AC-08.01-01构造正常数据，AC-08.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-08.02 metrics_svc.get_summary**
  - **名称/签名**：`get_summary(ctx:UserCtx,range:str,anchor_date:str)`。
  - **职责/来源**：指标摘要；US-08.02、AC-08.02-01/02/03。
  - **输入**：ctx:UserCtx,range:str,anchor_date:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{pv:int,uv:int,faq_hit_rate:float,coverage:float,knowledge_count:int,error_rate:float,unknown_usage_count:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 按上海日或ISO周接受时间归属。
    2. 唯一请求算PV/UV。
    3. 有效FAQ加授权RAG算覆盖。
  - **调用方**：GET /api/dashboard/summary。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：dashboard:view；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：零分母0并显示无样本；迟到终态可修订聚合。
  - **验证**：用AC-08.02-01构造正常数据，AC-08.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-08.02 `usemetrics_svc.submitGetSummary(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/dashboard/summary→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-08.03 metrics_svc.get_charts**
  - **名称/签名**：`get_charts(ctx:UserCtx,range:str,anchor_date:str,top_n:int)`。
  - **职责/来源**：六类图表排行；US-08.03、AC-08.03-01/02/03。
  - **输入**：ctx:UserCtx,range:str,anchor_date:str,top_n:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{traffic:list[dict],questions:list[dict],knowledge_heat:list[dict],usage:list[dict],latency:list[dict],knowledge_counts:dict}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 聚合访问、知识数、高频问题、每轮去重引用、分类型用量、成功失败延迟分布。
  - **调用方**：GET /api/dashboard/charts。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：dashboard:view；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：top_n1–50；无样本延迟null；知识标题需当前读权；跨用户问题仅返回脱敏主题计数，详见API契约。
  - **验证**：用AC-08.03-01构造正常数据，AC-08.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-08.03 `usemetrics_svc.submitGetCharts(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/dashboard/charts→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-08.04 metrics_svc.search_audit**
  - **名称/签名**：`search_audit(ctx:UserCtx,request_id:int|null,action:str|null,page:int,size:int)`。
  - **职责/来源**：审计查询；US-08.04、AC-08.04-01/02/03。
  - **输入**：ctx:UserCtx,request_id:int|null,action:str|null,page:int,size:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[dict],total:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 分项检查操作管理码。
    2. 问答正文仅本人且当前来源可读。
    3. 指标权限不等于全文权限。
    4. 脱敏记录。
  - **调用方**：GET /api/audit。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：登录态+分项授权；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：读取审计本身留痕；无全局正文旁路。
  - **验证**：用AC-08.04-01构造正常数据，AC-08.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-08.04 `usemetrics_svc.submitSearchAudit(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/audit→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

### M09 模型配置与运行控制

- 归属：backend/app/services/config_svc.py；前端按本模块页面/composable组织。

- **F-09.01 config_svc.read_config**
  - **名称/签名**：`read_config(ctx:UserCtx)`。
  - **职责/来源**：配置查询；US-09.01、AC-09.01-01/02/03。
  - **输入**：ctx:UserCtx。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{revision:int,models:dict,thresholds:dict,limits:dict,key_configured:bool}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 读当前配置版本，密钥仅显示是否设置。
  - **调用方**：GET /api/model-config。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:model；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：部署密钥由运行时注入，不从接口读回。
  - **验证**：用AC-09.01-01构造正常数据，AC-09.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-09.01 `useconfig_svc.submitReadConfig(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /api/model-config→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-09.02 config_svc.update_config**
  - **名称/签名**：`update_config(ctx:UserCtx,patch:dict,expected_revision:int)`。
  - **职责/来源**：保存配置；US-09.02、AC-09.02-01/02/03。
  - **输入**：ctx:UserCtx,patch:dict,expected_revision:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{revision:int}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 白名单字段范围能力校验。
    2. 事务更新审计。
    3. 新请求绑定新版本。
    4. 安全重排调度。
  - **调用方**：PATCH /api/model-config。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：sys:model；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：embedding变更必须重建，禁止直接混查不同向量空间。
  - **验证**：用AC-09.02-01构造正常数据，AC-09.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-09.02 `useconfig_svc.submitUpdateConfig(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求PATCH /api/model-config→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-09.03 config_svc.probe_provider**
  - **名称/签名**：`probe_provider(ctx:UserCtx,provider:str)`。
  - **职责/来源**：连通探测；US-09.03、AC-09.03-01/02/03。
  - **输入**：ctx:UserCtx,provider:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{ok:bool,latency_ms:int,error_code:str|null}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 只对允许配置地址最小探测。
    2. 计量及限流。
    3. 返回安全错误分类。
  - **调用方**：POST /api/model-config/probe。
  - **下游调用**：H02→H11/H12/H13（按类型）。
  - **权限/事务**：sys:model；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：禁止任意URL访问；有调用成本提示。
  - **验证**：用AC-09.03-01构造正常数据，AC-09.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-09.03 `useconfig_svc.submitProbeProvider(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求POST /api/model-config/probe→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

- **F-09.04 config_svc.readiness**
  - **名称/签名**：`readiness(now:datetime)`。
  - **职责/来源**：就绪检查；US-09.04、AC-09.04-01/02/03。
  - **输入**：now:datetime。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{ready:bool,checks:dict[str,bool],version:str}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 检查DB、索引加载、执行器和配置。
    2. health单独只看进程。
  - **调用方**：GET /ready。
  - **下游调用**：凭据校验或受信任务上下文 → 对应业务仓储；公开登录/刷新不调用H02，内部任务验证租约和登记来源。
  - **权限/事务**：内部探针；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：外部瞬断不导致无限重启；探针不含secret。
  - **验证**：用AC-09.04-01构造正常数据，AC-09.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。
  - **前端 FE-09.04 `useconfig_svc.submitReadiness(form: 对应输入去除ctx) -> Promise<对应输出>`**
    - 页面事件调用；表单校验→loading/防重复→H30请求GET /ready→更新列表/详情→finally退出loading。
    - 服务端revision取最近详情；client_*幂等键单次逻辑操作生成，重试复用；409刷新后重新审阅；失败保留输入并定位字段。
    - 流式入口使用H31订阅，不按JSON响应读取；引用点击H35；上传文件夹先H32，权限弹窗先H33。

### M10 前端交互与交付验收

- 归属：backend/app/services/delivery.py；前端按本模块页面/composable组织。

- **F-10.01 delivery.resolve_navigation**
  - **名称/签名**：`resolve_navigation(identity:UserCtx,routes:list[dict])`。
  - **职责/来源**：菜单路由权限；US-10.01、AC-10.01-01/02/03。
  - **输入**：identity:UserCtx,routes:list[dict]。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{items:list[{path:str,label:str}]}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 过滤菜单。
    2. 进入路由验证。
    3. 按钮校验。
    4. 401清身份403无权限。
  - **调用方**：前端路由。
  - **下游调用**：身份功能码→路由配置过滤。
  - **权限/事务**：前端；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：刷新身份时同步菜单，不以UI代替安全边界。
  - **验证**：用AC-10.01-01构造正常数据，AC-10.01-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-10.02 delivery.render_answer**
  - **名称/签名**：`render_answer(events:list[SseEvent],last_seq:int)`。
  - **职责/来源**：流式Markdown；US-10.02、AC-10.02-01/02/03。
  - **输入**：events:list[SseEvent],last_seq:int。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{text:str,html:str,last_seq:int,terminal:bool}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 事件序号去重。
    2. rAF批次输出。
    3. 禁原始HTML与危险协议。
    4. 文本代码高亮复制。
    5. 引用定位。
  - **调用方**：前端组件。
  - **下游调用**：H31→序号去重→安全Markdown渲染。
  - **权限/事务**：前端；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：坏帧进入恢复不静默跳过；末批必须flush。
  - **验证**：用AC-10.02-01构造正常数据，AC-10.02-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-10.03 delivery.replay_acceptance**
  - **名称/签名**：`replay_acceptance(scenario:str,run_id:UUID)`。
  - **职责/来源**：端到端场景；US-10.03、AC-10.03-01/02/03。
  - **输入**：scenario:str,run_id:UUID。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{passed:bool,steps:list[{id:str,expected:str,actual:str,passed:bool}]}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 测试环境建立组织知识。
    2. 财务权限负向。
    3. 客服聚类审核命中缺口补档。
    4. 指标核对。
  - **调用方**：验收执行。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：验收工具；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：不写生产；不以链式相似度造假通过聚类。
  - **验证**：用AC-10.03-01构造正常数据，AC-10.03-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

- **F-10.04 delivery.verify_release**
  - **名称/签名**：`verify_release(release_id:str,environment:str)`。
  - **职责/来源**：交付恢复验证；US-10.04、AC-10.04-01/02/03。
  - **输入**：release_id:str,environment:str。类型见§1；ID正整数，普通文本trim后校验，密码不trim不截断，可空仅标null字段。
  - **输出**：`{passed:bool,image_digests:list[str],migration_revision:str,backup_id:str,errors:list[str]}`；list/iterator结果按上述原类型返回，不重复包对象。
  - **处理逻辑**：
    1. 构建版本镜像。
    2. 迁移。
    3. 首版单实例维护窗口。
    4. 备份DB原文及索引重建资料。
    5. 恢复后回放。
  - **调用方**：交付执行。
  - **下游调用**：H02功能检查 → 对应业务实体Repository（按处理逻辑的字段/状态条件执行）。
  - **权限/事务**：运维；变更与审计同事务，外部副作用持久任务化；查询不额外创建业务记录。
  - **边界及错误**：不承诺未设计的蓝绿零停机；已确认格式范围与待定容量均不可冒充已实测验收。
  - **验证**：用AC-10.04-01构造正常数据，AC-10.04-02构造失败/边界；涉及权限追加拒绝与撤权测试，涉及状态追加重试和竞争测试。

## 4. 基础函数契约

- **H01 auth_core.authenticate**
  - **签名**：`auth_core.authenticate(access_token:SecretStr) -> UserCtx`。
  - **职责**：验签用途过期。
  - **输入**：access_token:SecretStr；类型见§1。
  - **输出**：UserCtx。
  - **逻辑**：
    1. 验签用途过期。
    2. 核对持久会话撤销及用户状态。
    3. 加载当前直属部门角色功能码。
  - **调用方**：所有受保护路由。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：失效401，存储失败503；禁止token旧权限放行。
  - **实现分工（2026-09-16）**：① **纯令牌原语**在 `app/core/security.py`（`decode_token`，验签/用途/过期）；② **DB 读取与身份装配**在 `app/services/auth_svc.py::authenticate`（按 `sid` 核对 `auth_session.revoked_at`、按 `user.enabled` 判停用、装配 `dept_id`/`role_ids`/`permission_codes`/`identity_revision`）；③ **FastAPI 依赖包装**在 `app/api/deps.py::get_current_ctx`（取 `Authorization: Bearer`、映射 401 错误码）。路由层不直接解析令牌。
  - **`sid` 的必要性**：契约要求 H01"核对持久会话撤销"。进程内撤销表时代这一步无法实现；改成持久表后，**令牌若不携带会话标识就无处可核**——access token 将在注销后继续有效至多 2 小时。因此 `sid` 是"注销立即生效"的必要条件，而非可选优化。它不是权限快照（不含角色/部门/权限码），不违反 payload 最小化原则。

- **H02 auth_core.require_permission**
  - **签名**：`auth_core.require_permission(ctx:UserCtx,code:str) -> None`。
  - **职责**：检查当前功能码集合。
  - **输入**：ctx:UserCtx,code:str；类型见§1。
  - **输出**：None。
  - **逻辑**：
    1. 检查当前功能码集合。
    2. 缺失抛403。
  - **调用方**：所有服务入口。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：功能权限不代替数据权限。

- **H03 permission.judge**
  - **签名**：`permission.judge(ctx:UserCtx,unit:UnitState) -> DenyReason|null`（null 表示放行；返回值为**内部**原因码，禁止下发客户端）。
  - **职责**：检查停用与删除状态，并按四维 OR 判定是否放行。
  - **输入**：ctx:UserCtx,unit:UnitState；类型见§1，字段名映射见§1。
  - **输出**：DenyReason|null。
  - **逻辑**：
    1. 检查 enabled 与 is_deleted（停用或已删除直接拒绝）。
    2. global OR 直属部门精确匹配 OR 角色交集 OR 个人包含；部门维度不继承祖先或子孙。
    3. 四维全未命中时，原因码按 **部门 → 角色 → 个人 → 默认拒绝** 依次选取（顺序稳定，供受控审计复现；该明细不下发客户端）。
  - **验收矩阵**：必须覆盖 global / 直属部门 / 角色 / 个人 **四维的 16 种布尔组合**（PRD §1.4「四维16种布尔组合」单元验收）——每一维各自"命中/未命中"，断言"至少一维命中即放行、四维全不命中即拒绝"，并单独断言全空时的 `DENY_DEFAULT`。
  - **调用方**：authorize_units。
  - **下游/依赖**：纯计算，不读库不调用模型。
  - **边界/失败**：无创建者/超管/祖先例外；异常不allow；返回值只进受控审计，不进入客户端响应。
  - **实现状态**：判定侧已实现（停用、已删除、四维 OR；实现类型 `UnitState` 与§1字段逐一致）；H04 的读库与 `unavailable` 分桶待 M1-6。

- **H04 permission.authorize_units**
  - **签名**：`permission.authorize_units(ctx:UserCtx,unit_ids:list[int]) -> allowed:list[int],denied:list[int],unavailable:list[int],versions:dict`。
  - **职责**：批量MySQL读取当前ACL/状态/版本。
  - **输入**：ctx:UserCtx,unit_ids:list[int]；类型见§1。
  - **输出**：allowed:list[int],denied:list[int],unavailable:list[int],versions:dict。
  - **逻辑**：
    1. 批量MySQL读取当前ACL/状态/版本。
    2. 逐项judge分类。
  - **调用方**：检索/正文/引用。
  - **纯计算子步骤**：`permission.filter_units(ctx:UserCtx,units:list[UnitState]) -> FilterResult`。本步**不读库**，只对已加载的单元逐项调用 H03 并切分为 `allowed_ids`/`denied_ids`（类型见§1）。ARCHITECTURE §2（授权引擎）要求"纯 judge 只计算、authorize_units 负责读库"，本子步骤即该分工中纯计算一侧的落地点；`denied_ids` 只允许写入受控审计，不得下发客户端。
  - **索引可用性子步骤**：`permission.is_retrievable(unit:UnitState,*,chunk_version:int) -> bool`。放行条件为 `enabled`、未删除、`index_status=indexed` 且 `content_version=indexed_version=chunk_version`（ARCHITECTURE §4.1）。不满足者归入 `unavailable`，**必须与 `denied` 分开**——无权、无证据、索引不可用、服务失败不可混为一类；Milvus 标量过滤只作预筛，不替代本步复核。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：向量字段只预筛，不做最终授权；`unavailable`（索引不可用）必须与"无权"分开，不能混入 denied。

- **H05 permission.authorize_sources**
  - **签名**：`permission.authorize_sources(ctx:UserCtx,sources:list[SourceRef]) -> allowed:bool`。
  - **职责**：要求来源非空。
  - **输入**：ctx:UserCtx,sources:list[SourceRef]；类型见§1。
  - **输出**：allowed:bool。
  - **逻辑**：
    1. 要求来源非空。
    2. 每个来源当前有效且judge允许，来源间AND。
  - **调用方**：FAQ/历史/引用/事件恢复。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：任一个失败整份派生答案不返回。

- **H06 permission.sanitize_denied**
  - **签名**：`permission.sanitize_denied(has_denied:bool) -> RestrictedNotice|null`。
  - **职责**：部分无权时只返回 ACCESS_RESTRICTED 与固定 message；未发生拒绝返回 null。
  - **输入**：has_denied:bool；类型见§1。
  - **输出**：RestrictedNotice|null。
  - **逻辑**：
    1. 未发生拒绝 → 返回 null（不污染全部有权路径）。
    2. 发生拒绝 → RestrictedNotice(ACCESS_RESTRICTED, PARTIAL_RESTRICTED_NOTICE)。
  - **调用方**：run_answer（部分无权：仅用有权证据并追加固定提示）。
  - **下游/依赖**：纯函数；入参只有一个布尔量，从签名上杜绝挟带受限 ID/标题/数量。
  - **边界/失败**：不含ID标题ACL名称数量；全部无权终态改用 H36。
  - **实现状态**：已实现（审计 PA-04/PA-05 于 2026-09-16 关闭）。

- **H07 permission.guard_output**
  - **签名**：`permission.guard_output(request_id:int,events:list[SseEvent]) -> allowed:bool,events:list[SseEvent],stop_reason:str|null`。
  - **职责**：与权限变更共享短临界区。
  - **输入**：request_id:int,events:list[SseEvent]；类型见§1。
  - **输出**：allowed:bool,events:list[SseEvent],stop_reason:str|null。
  - **逻辑**：
    1. 与权限变更共享短临界区。
    2. 复核最新身份来源取消标记。
    3. 允许再入发送队列。
  - **调用方**：stream_events。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：提交生效后不新增发送，已发送无法撤回。

- **H08 retrieval.retrieve_authorized**
  - **签名**：`retrieval.retrieve_authorized(ctx:UserCtx,query:str,config:dict) -> RetrievalResult`。
  - **职责**：两路各20召回。
  - **输入**：ctx:UserCtx,query:str,config:dict；类型见§1。
  - **输出**：RetrievalResult。
  - **逻辑**：
    1. 两路各20召回。
    2. MySQL版本复核。
    3. authorize_units。
    4. 必要扩大各100。
    5. 有权候选融合重排Top5。
  - **调用方**：run_answer/verify_gap。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：无权无证据服务故障分开；有限窗口不证明全库不存在。

- **H09 retrieval.rrf_fuse**
  - **签名**：`retrieval.rrf_fuse(rankings:list[list[ScoredChunk]],k:int) -> list[ScoredChunk]`。
  - **职责**：按chunk_id累加1/(k+rank)，rank从1。
  - **输入**：rankings:list[list[ScoredChunk]],k:int；类型见§1。
  - **输出**：list[ScoredChunk]。
  - **逻辑**：
    1. 按chunk_id累加1/(k+rank)，rank从1。
    2. 同分稳定排序。
    3. 保留原分。
  - **调用方**：retrieve_authorized。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：k>0；RRF不作置信度。

- **H10 retrieval.build_messages**
  - **签名**：`retrieval.build_messages(ctx:UserCtx,history:list[dict],evidence:list[ScoredChunk],question:str,budget:int) -> list[{role:str,content:str}]`。
  - **职责**：历史来源再授权。
  - **输入**：ctx:UserCtx,history:list[dict],evidence:list[ScoredChunk],question:str,budget:int；类型见§1。
  - **输出**：list[{role:str,content:str}]。
  - **逻辑**：
    1. 历史来源再授权。
    2. 保留完整最近轮次。
    3. 证据编号。
    4. 系统约束及当前问题不可截掉。
  - **调用方**：run_answer。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：无来源旧答案不回填；预算不足受控拒绝。

- **H11 providers.embed_batches**
  - **签名**：`providers.embed_batches(texts:list[str],config:dict) -> vectors:list[list[float]],usage:Usage,model_version:str`。
  - **职责**：按10条与token上限分批。
  - **输入**：texts:list[str],config:dict；类型见§1。
  - **输出**：vectors:list[list[float]],usage:Usage,model_version:str。
  - **逻辑**：
    1. 按10条与token上限分批。
    2. 按返回index对齐。
    3. 校验数量维度有限值。
  - **调用方**：pipeline/FAQ/mining。
  - **下游/依赖**：允许地址的模型服务与用量记录。
  - **边界/失败**：超长重新切片不静默截断；限流最多3次退避。

- **H12 providers.rerank**
  - **签名**：`providers.rerank(query:str,chunks:list[ScoredChunk],top_n:int,config:dict) -> list[ScoredChunk]`。
  - **职责**：仅接收授权正文。
  - **输入**：query:str,chunks:list[ScoredChunk],top_n:int,config:dict；类型见§1。
  - **输出**：list[ScoredChunk]。
  - **逻辑**：
    1. 仅接收授权正文。
    2. 空文过滤保留索引映射。
    3. 校验返回下标。
  - **调用方**：retrieve_authorized。
  - **下游/依赖**：允许地址的模型服务与用量记录。
  - **边界/失败**：失败显式降级为已授权融合排序。

- **H13 providers.stream_llm**
  - **签名**：`providers.stream_llm(messages:list[dict],config:dict,cancel:CancelSignal) -> AsyncIterator[ModelDelta]`。
  - **职责**：绑定配置版本。
  - **输入**：messages:list[dict],config:dict,cancel:CancelSignal；类型见§1。
  - **输出**：AsyncIterator[ModelDelta]。
  - **逻辑**：
    1. 绑定配置版本。
    2. 处理文本真实用量和截止时间。
    3. 协作取消。
  - **调用方**：run_answer。
  - **下游/依赖**：允许地址的模型服务与用量记录。
  - **边界/失败**：首段输出后不透明重试；未知用量null。

- **H14 tasks.claim_task**
  - **签名**：`tasks.claim_task(task_id:int,worker_id:str,now:datetime) -> TaskLease|null`。
  - **职责**：条件领取queued/到期retry_wait。
  - **输入**：task_id:int,worker_id:str,now:datetime；类型见§1。
  - **输出**：TaskLease|null。
  - **逻辑**：
    1. 条件领取queued/到期retry_wait。
    2. 设置token和到期。
  - **调用方**：任务执行器。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：并发只能一个成功。

- **H15 tasks.renew_lease**
  - **签名**：`tasks.renew_lease(task_id:int,lease_token:UUID,now:datetime) -> bool`。
  - **职责**：条件匹配running与token延长到期。
  - **输入**：task_id:int,lease_token:UUID,now:datetime；类型见§1。
  - **输出**：bool。
  - **逻辑**：
    1. 条件匹配running与token延长到期。
  - **调用方**：run_pipeline。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：失败停止写入和提交。

- **H16 tasks.upsert_version**
  - **签名**：`tasks.upsert_version(unit_id:int,version:int,chunks:list[EmbeddedChunk],lease:TaskLease) -> expected_count:int,written_count:int,verified:bool`。
  - **职责**：主键unit:version:seq写入。
  - **输入**：unit_id:int,version:int,chunks:list[EmbeddedChunk],lease:TaskLease；类型见§1。
  - **输出**：expected_count:int,written_count:int,verified:bool。
  - **逻辑**：
    1. 主键unit:version:seq写入。
    2. 同版本复用持久切片。
    3. 核对数量。
  - **调用方**：run_pipeline。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：不按unit删除其他版本；部分写入不激活。

- **H17 tasks.activate_version**
  - **签名**：`tasks.activate_version(unit_id:int,version:int,lease:TaskLease) -> activated:bool,status:str`。
  - **职责**：短事务验证当前版本、未删、租约匹配。
  - **输入**：unit_id:int,version:int,lease:TaskLease；类型见§1。
  - **输出**：activated:bool,status:str。
  - **逻辑**：
    1. 短事务验证当前版本、未删、租约匹配。
    2. 更新indexed_version并完成任务。
    3. 倒排增量同步。
  - **调用方**：run_pipeline。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：旧任务superseded；检索仍复核DB。

- **H18 tasks.cleanup_deleted**
  - **签名**：`tasks.cleanup_deleted(unit_id:int,deletion_id:int) -> removed:int,status:str,error_code:str|null`。
  - **职责**：确认墓碑。
  - **输入**：unit_id:int,deletion_id:int；类型见§1。
  - **输出**：removed:int,status:str,error_code:str|null。
  - **逻辑**：
    1. 确认墓碑。
    2. 清理版本向量原文。
    3. 保存完成记录。
    4. 失败重试。
  - **调用方**：清理执行器。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：共享文件引用未归零不删除；不复活。

- **H19 parsing.parse_document**
  - **签名**：`parsing.parse_document(path:ServerPath,format:str) -> text:str,locations:list[Location]`。
  - **职责**：按pdf/docx/md/txt提取文本及页段定位。
  - **输入**：path:ServerPath,format:str；类型见§1。
  - **输出**：text:str,locations:list[Location]。
  - **逻辑**：
    1. 按pdf/docx/md/txt提取文本及页段定位。
    2. 加密损坏独立错误。
  - **调用方**：run_pipeline。
  - **下游/依赖**：格式解析器/token计数器，不调用LLM。
  - **边界/失败**：短有效文本不能仅凭少于200字判空。

- **H20 parsing.clean_text**
  - **签名**：`parsing.clean_text(text:str,locations:list[Location]) -> text:str,locations:list[Location],warnings:list[str]`。
  - **职责**：Unicode规范化。
  - **输入**：text:str,locations:list[Location]；类型见§1。
  - **输出**：text:str,locations:list[Location],warnings:list[str]。
  - **逻辑**：
    1. Unicode规范化。
    2. 换行统一。
    3. 控制符清理。
    4. 行尾空白。
    5. 过多空行。
    6. 跨页重复页眉脚检测。
    7. 保守断行拼接。
    8. 空文本校验。
  - **调用方**：run_pipeline。
  - **下游/依赖**：格式解析器/token计数器，不调用LLM。
  - **边界/失败**：不删表格数字条款；保留来源偏移映射。

- **H21 parsing.split_text**
  - **签名**：`parsing.split_text(text:str,locations:list[Location],size:int,overlap:int) -> list[{seq:int,text:str,token_count:int,location:Location}]`。
  - **职责**：标题段落优先。
  - **输入**：text:str,locations:list[Location],size:int,overlap:int；类型见§1。
  - **输出**：list[{seq:int,text:str,token_count:int,location:Location}]。
  - **逻辑**：
    1. 标题段落优先。
    2. 长段token窗口。
    3. 默认500/50。
    4. 保存定位。
  - **调用方**：run_pipeline。
  - **下游/依赖**：格式解析器/token计数器，不调用LLM。
  - **边界/失败**：0<=overlap<size；字符不当token。

- **H22 mining.claim_logs**
  - **签名**：`mining.claim_logs(run_id:int,pipeline_version:str,limit:int) -> MiningBatch`。
  - **职责**：领取终态未消费记录，唯一(log_id,pipeline_version)加租约。
  - **输入**：run_id:int,pipeline_version:str,limit:int；类型见§1。
  - **输出**：MiningBatch。
  - **逻辑**：
    1. 领取终态未消费记录，唯一(log_id,pipeline_version)加租约。
  - **调用方**：run_mining。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：不只取最大ID以免漏晚提交。

- **H23 mining.cluster_questions**
  - **签名**：`mining.cluster_questions(questions:list[QuestionVector],threshold:float) -> list[QuestionCluster]`。
  - **职责**：按部门来源边界分组。
  - **输入**：questions:list[QuestionVector],threshold:float；类型见§1。
  - **输出**：list[QuestionCluster]。
  - **逻辑**：
    1. 按部门来源边界分组。
    2. 相似合并后校验每项与代表。
    3. 保留成员。
  - **调用方**：run_mining。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：禁止仅靠A-B-C相似链跨主题聚类。

- **H24 mining.draft_candidate**
  - **签名**：`mining.draft_candidate(cluster:QuestionCluster,sources:list[SourceRef]) -> FaqDraft`。
  - **职责**：有效来源正文起草。
  - **输入**：cluster:QuestionCluster,sources:list[SourceRef]；类型见§1。
  - **输出**：FaqDraft。
  - **逻辑**：
    1. 有效来源正文起草。
    2. 保存模型版本和依据。
    3. 稳定job_key复用外部结果。
  - **调用方**：run_mining。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：频次初值5；无来源不产生可发布候选。

- **H25 mining.commit_consumption**
  - **签名**：`mining.commit_consumption(batch:MiningBatch,drafts:list[FaqDraft]) -> consumed:int,created:int`。
  - **职责**：事务写候选归簇与消费完成，校验租约。
  - **输入**：batch:MiningBatch,drafts:list[FaqDraft]；类型见§1。
  - **输出**：consumed:int,created:int。
  - **逻辑**：
    1. 事务写候选归簇与消费完成，校验租约。
  - **调用方**：run_mining。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：失败回滚消费标识；模型调用事务外。

- **H26 chat_store.append_event**
  - **签名**：`chat_store.append_event(request_id:int,event:str,payload:dict) -> SseEvent`。
  - **职责**：分配单调seq。
  - **输入**：request_id:int,event:str,payload:dict；类型见§1。
  - **输出**：SseEvent。
  - **逻辑**：
    1. 分配单调seq。
    2. 唯一键持久化。
    3. 提交后广播。
  - **调用方**：run_answer/finalize_request。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：重放前再次授权；敏感审计对象不直传。

- **H27 chat_store.get_request**
  - **签名**：`chat_store.get_request(ctx:UserCtx,request_id:int) -> status:str,last_seq:int,answer:str|null,restricted:bool`。
  - **职责**：归属查询及来源复核，返回安全快照。
  - **输入**：ctx:UserCtx,request_id:int；类型见§1。
  - **输出**：status:str,last_seq:int,answer:str|null,restricted:bool。
  - **逻辑**：
    1. 归属查询及来源复核，返回安全快照。
  - **调用方**：前端恢复/取消。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：他人404；受限占位。

- **H28 chat_store.list_sessions**
  - **签名**：`chat_store.list_sessions(ctx:UserCtx,page:int,size:int) -> items:list[{id:int,title:str,updated_at:datetime}],total:int`。
  - **职责**：owner过滤后分页。
  - **输入**：ctx:UserCtx,page:int,size:int；类型见§1。
  - **输出**：items:list[{id:int,title:str,updated_at:datetime}],total:int。
  - **逻辑**：
    1. owner过滤后分页。
    2. 更新时间及ID稳定排序。
  - **调用方**：会话侧栏。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：无其他用户标题。

- **H29 chat_store.recover_requests**
  - **签名**：`chat_store.recover_requests(now:datetime) -> failed:int`。
  - **职责**：租约过期running条件终结failed，未知用量标记。
  - **输入**：now:datetime；类型见§1。
  - **输出**：failed:int。
  - **逻辑**：
    1. 租约过期running条件终结failed，未知用量标记。
  - **调用方**：启动恢复。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：不自动再次付费生成；重复幂等。

- **H30 frontend.api_request**
  - **签名**：`frontend.api_request(method:str,path:str,body:Json|null) -> Promise[{code:str,message:str,data:Json,request_id:str}]`。
  - **职责**：附令牌。
  - **输入**：method:str,path:str,body:Json|null；类型见§1。
  - **输出**：Promise[{code:str,message:str,data:Json,request_id:str}]。
  - **逻辑**：
    1. 附令牌。
    2. 401共用一次refresh。
    3. 映射错误。
    4. 有幂等键才自动重试写请求。
  - **调用方**：所有前端业务函数。
  - **下游/依赖**：浏览器fetch/流/组件状态与对应后端API。
  - **边界/失败**：禁止无限刷新与无键重复写。

- **H31 frontend.parse_sse**
  - **签名**：`frontend.parse_sse(reader:ByteReader,last_seq:int) -> AsyncIterator[SseEvent]`。
  - **职责**：流式UTF8缓存。
  - **输入**：reader:ByteReader,last_seq:int；类型见§1。
  - **输出**：AsyncIterator[SseEvent]。
  - **逻辑**：
    1. 流式UTF8缓存。
    2. LF/CRLF帧解析。
    3. 多行data拼接。
    4. seq去重。
  - **调用方**：stream_events前端。
  - **下游/依赖**：浏览器fetch/流/组件状态与对应后端API。
  - **边界/失败**：坏JSON或缺序触发恢复，不静默跳过。

- **H32 frontend.expand_drop**
  - **签名**：`frontend.expand_drop(entries:list[BrowserEntry]) -> list[{file:UploadFile,relative_path:str}]`。
  - **职责**：递归枚举目录。
  - **输入**：entries:list[BrowserEntry]；类型见§1。
  - **输出**：list[{file:UploadFile,relative_path:str}]。
  - **逻辑**：
    1. 递归枚举目录。
    2. 保留展示路径。
    3. 清单预校验。
  - **调用方**：批量导入。
  - **下游/依赖**：浏览器fetch/流/组件状态与对应后端API。
  - **边界/失败**：不支持目录时多选提示；服务端仍验证。

- **H33 frontend.normalize_acl**
  - **签名**：`frontend.normalize_acl(global:bool,depts:list[int],roles:list[int],users:list[int]) -> global:bool,depts:list[int],roles:list[int],users:list[int]`。
  - **职责**：实体去重。
  - **输入**：global:bool,depts:list[int],roles:list[int],users:list[int]；类型见§1。
  - **输出**：global:bool,depts:list[int],roles:list[int],users:list[int]。
  - **逻辑**：
    1. 实体去重。
    2. 保留非global选择。
    3. 全空提示默认拒绝。
  - **调用方**：权限弹窗。
  - **下游/依赖**：浏览器fetch/流/组件状态与对应后端API。
  - **边界/失败**：不显示创建者超管例外。

- **H34 faq_store.list_faqs**
  - **签名**：`faq_store.list_faqs(ctx:UserCtx,status:str|null,q:str,page:int,size:int) -> items:list[{id:int,question:str,answer:str,status:str,frequency:int,confidence:float,source_refs:list[SourceRef],hit_count:int}],total:int`。
  - **职责**：检查faq:review或publish及全部来源读权。
  - **输入**：ctx:UserCtx,status:str|null,q:str,page:int,size:int；类型见§1。
  - **输出**：items:list[{id:int,question:str,answer:str,status:str,frequency:int,confidence:float,source_refs:list[SourceRef],hit_count:int}],total:int。
  - **逻辑**：
    1. 检查faq:review或publish及全部来源读权。
    2. 授权条件分页。
  - **调用方**：FAQ候选及已发布页。
  - **下游/依赖**：按逻辑指定的权威仓储或公共函数，禁止越层直传敏感对象。
  - **边界/失败**：不先分页再过滤造成总数泄露。

- **H35 frontend.open_citation**
  - **签名**：`frontend.open_citation(request_id:int,no:int) -> Promise[{title:str,text:str,location:Location}]`。
  - **职责**：调用read_citation。
  - **输入**：request_id:int,no:int；类型见§1。
  - **输出**：Promise[{title:str,text:str,location:Location}]。
  - **逻辑**：
    1. 调用read_citation。
    2. 展示原文位置。
    3. 失效显示不可用。
  - **调用方**：引用卡片。
  - **下游/依赖**：浏览器fetch/流/组件状态与对应后端API。
  - **边界/失败**：不用旧缓存绕过再次鉴权。

- **H36 permission.restricted_refusal**
  - **签名**：`permission.restricted_refusal() -> RestrictedNotice`。
  - **职责**：全部无权时的固定拒绝提示（`result_type=access_restricted`）。
  - **输入**：无。
  - **输出**：RestrictedNotice。
  - **逻辑**：
    1. 返回 RestrictedNotice(ACCESS_RESTRICTED, ACCESS_RESTRICTED_NOTICE)。
  - **调用方**：run_answer（allowed 为空且存在被拒候选时）。
  - **下游/依赖**：纯函数且**无入参**——不存在"顺手"把受限元数据传进来的可能。
  - **边界/失败**：固定文案，不含ID标题ACL名称数量；该终态**不调用生成 Provider**（DESIGN_REVISION §2.2「全部无权」）。
  - **实现状态**：已实现（审计 PA-04/PA-05 于 2026-09-16 关闭）。

