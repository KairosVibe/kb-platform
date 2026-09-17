# 权限基座与 R3 安全契约一致性审计

> R3 定位与交付：通用知识管理与 AI 问答平台，用于真实工程落地及简历展示；不转为电商/售后专用产品。继承 R2 功能编号和安全契约。详见 [交付、评测与文档确认](DELIVERY-EVALUATION.md)。

版本：2026-09-16 R3。状态：**审计记录 + 修正台账；PA-01—PA-15 已全部关闭**。步骤 1—6 完成判定修正；PA-06 的 `authorize_units` 读库与 PA-07 的持久化仓储已随 `models/`、`db/`、`services/` 落地，**并在真实 MySQL 8.0.26 上通过集成验证**（`pytest` 245 passed，含 17 项集成用例；PA-07 的判定性证据是跨进程重启后撤销仍生效）。一致性门禁的**已知差异清单已清零**（exit 0）。全部关闭结论均附命令与实际输出，见 §5 与本文件各 PA 行。

## 1. 范围、方法与边界

审计对象（实际存在，非设计稿）：

| 文件 | 性质 |
|---|---|
| `backend/app/core/config.py` | 部署配置 |
| `backend/app/core/permissions.py` | 14 权限码静态表 |
| `backend/app/core/response.py` | 统一响应包与错误码表 |
| `backend/app/core/security.py` | 密码哈希与 JWT |
| `backend/app/core/logging.py` | 结构化日志 |
| `backend/app/engines/permission.py` | **四维权限判定引擎与受限输出契约（审计重点）** |
| `backend/tests/test_permission_truth_table.py` | 真值矩阵与受限输出契约测试 |
| `backend/scripts/check_doc_code_sync.py` | 文档-代码一致性核对 |

判定基准（冲突时以左侧优先）：[DESIGN_REVISION](DESIGN_REVISION.md) §2 → [ARCHITECTURE](ARCHITECTURE.md) §3/§4.1 → [PRD](PRD.md) §1/§1.2/§1.4 → [FUNCTION-MAP](FUNCTION-MAP.md) §1/§2/§2.1/§4 → [API-CONTRACTS](API-CONTRACTS.md) §1。

方法：对判定函数逐分支静态比对 + 执行测试与一致性脚本取实测结果；修正后同样以实测结果作为关闭证据。

**未执行**：OpenAPI/DDL 生成、真实 MySQL/Milvus 集成测试、模型评测、压测与恢复演练。涉及集成环境的结论（PA-06、PA-07）须在集成环境复验。

编号 `PA-nn`（Permission Audit）供 D1 修正清单引用。

## 2. 执行证据

| 命令（工作目录 `backend/`） | 首次实测 | PA-13 修正后 | 步骤 1 后 | 步骤 2 后 | 步骤 3 后 | 步骤 4 后 | 步骤 5 后 | 步骤 6 后 |
|---|---|---|---|---|---|---|---|---|
| `python -m pytest` | 31 passed | 31 passed | 33 passed | 40 passed | 51 passed | 67 passed | 99 passed | **124 passed** |
| `python scripts/check_doc_code_sync.py` | FAIL，exit 1 | exit 0，已知 28 处 | exit 0，已知 15 处 | exit 0，已知 14 处 | exit 0，已知 14 处 | exit 0，已知 12 处 | exit 0，已知 0 处 | **exit 0，已知 0 处**（8 项检查全 OK，无豁免） |

首次脚本失败的两条输出：

1. `PRD 未列出这些权限码: [14 项全部]`
2. `代码定义但 PRD 未使用（可能冗余）: [DENY_DEFAULT, DENY_DEPT, DENY_DISABLED, DENY_ROLE, DENY_USER]`

两条均**不是代码缺陷**，而是脚本把"正则没匹配上"当成"检查通过"：`权限码全集（N 个）` 与带反引号的 `DENY_*` 在新 PRD 中都不存在，三个锚点静默消失。处置见 §5.1。

## 3. 冲突清单与关闭状态

### 3.1 安全红线

| 编号 | 冲突内容 | 状态 |
|---|---|---|
| PA-01 | `UserCtx.is_super` 旁路全部数据权限；`SUPER_ADMIN_ROLE_CODE="sys_admin"` 标记该旁路 | **已关闭 2026-09-16**（步骤 1）：字段与常量删除，并加结构性断言防回归 |
| PA-02 | 创建者自动获得读取权 | **已关闭 2026-09-16**（步骤 1）：删除 creator 判定分支，`creator_id` 降级为审计字段 |
| PA-03 | `dept_chain`=本部门∪全部祖先，与 `perm_depts` 求交集（祖先/子孙隐式继承） | **已关闭 2026-09-16**（步骤 1）：改为直属部门精确匹配，覆盖验收用例 A08 |
| PA-04 | `DeniedUnit` 携带受限单元 `title` 与 `missing_hint`；`describe_missing()` 输出"需：人力资源部 或 管理层角色"，违反 DESIGN_REVISION §2.2"不含受限 ID、标题、摘要、部门名单" | **已关闭 2026-09-16**（步骤 2）：`DeniedUnit`/`describe_missing` 整体删除，`FilterResult` 只返回 ID |
| PA-05 | 对外返回 5 个明细原因码；契约要求的 `ACCESS_RESTRICTED` 在代码中无字面量定义 | **已关闭 2026-09-16**（步骤 2）：新增 `ClientReasonCode.ACCESS_RESTRICTED`，内部 `DenyReason` 降级为仅审计可用 |
| PA-06 | 判定类型缺 `is_deleted`/`content_version`/`indexed_version`/`index_status`/`acl_version`；`UserCtx` 缺 `session_id`/`permission_codes`/`identity_revision`；判定无法区分"已删除"与"索引过期"单元 | **判定侧已关闭 2026-09-16**（步骤 3，见 §5.4）：类型更名 `UnitState` 并补齐 FUNCTION-MAP §1 全部字段，`UserCtx` 六字段齐备，新增 `DENY_DELETED` 墓碑拦截与 `is_retrievable` 索引可用性判定。**已关闭 2026-09-16**（判定侧 + 读库 + 真实 MySQL 验证）：`app/services/authz_svc.py` 的 `authorize_units`/`assemble_unit_states`/`classify_units` 已落地，依赖 `app/models/`（**36 表，实体字典全部**）与 `app/db/` 层。证据：① 离线分桶用例 16 项（`tests/test_authz_svc.py`）；② **真实 MySQL 端到端** 2 项（`tests/integration/test_authorize_units_mysql.py`）——覆盖四维命中/未命中、墓碑、停用、索引未就绪（`pending` 与 `indexed_version < content_version` 两种）、不存在的 ID，且 `versions` 只含放行单元。全量 `pytest` 245 passed |
| PA-07 | `RefreshBlacklist` 为进程内 `dict` + 全局单例，重启丢失撤销 → 已注销令牌可重放 | **部分关闭 2026-09-16**（步骤 4，见 §5.5）：改为 `InMemoryRefreshRevocationStore`，**非 dev 环境构造即失败**（宁可启动失败也不让"重启即遗忘撤销"上生产），语义补全一次性消费与会话级幂等撤销。**DB 持久仓储已实现（2026-09-16，M1-6）**：`app/services/auth_svc.py` 的 `PersistentRefreshStore` 落表 `auth_session`/`refresh_token`，语义与进程内实现逐条对齐（`token_hash` 唯一挡重放、`consumed_at` 记消费、`revoked_at` 记会话级撤销、`replaced_by` 记轮换链），并用 SAVEPOINT 保证"重放"这一预期路径不会毁掉外层事务。**已关闭 2026-09-16**（真实 MySQL 集成验证，含跨进程重启）：证据 `tests/integration/test_refresh_store_mysql.py` **8 项全通过**，其中 `test_revocation_survives_process_restart` 用**五个独立解释器进程**依次执行 seed→verify→revoke→verify→consume——进程内实现不可能通过该用例，这是 PA-07 的判定性证据；`test_concurrent_refresh_only_one_wins` 用两条独立连接并发消费同一 `jti`，恰好一个成功（AC-01.02-01）。另修复并发下 MySQL 可能报死锁（1213）而非重复键（1062）的路径，败者返回 `False` 而非 500 |
| PA-08 | 4 个用例断言旧规则（创建者例外、祖先链命中、超管旁路） | **已关闭 2026-09-16**（步骤 1）：全部翻转为拒绝预期并新增结构性防线，未删除任何用例 |

### 3.2 契约不符

| 编号 | 位置 | 问题 | 状态 |
|---|---|---|---|
| PA-09 | `security.py` | 密码 >72 字节**静默截断**（`raw = raw[:72]`），与 PRD AC-01.01-02"显式拒绝，不截断"相反 | **已关闭 2026-09-16**（步骤 4，见 §5.5）：新增 `PasswordPolicyError`，创建时强制 12—72 字节、校验时 >72 字节**抛异常**（不再截断、也不退化为 `False`）；回归测试直接断言"前 72 字节相同的两个密码不会互相通过" |
| PA-10 | `config.py` | `local_bge` 被注释为"配额耗尽时的应急降级"，违反 DELIVERY-EVALUATION §5"不得直接混查不同向量空间" | **已关闭 2026-09-16**（步骤 4，见 §5.5）：删除降级表述，明确三项均为**部署期决定**；新增 `embedding_model_version`（provider:model:dim）作为向量空间标识，写入 `knowledge_version.embedding_model_version`，用于判定能否复用既有向量 |
| PA-11 | `response.py` | 状态码错配：`UNSUPPORTED_FORMAT`→400（应 415）、`INVALID_PERM_CODE`→400（应 422）、`SELF_LOCK`→400（应 409）、通用参数→400（应 422）；**缺** 410/415/422/503 四类状态；**缺** `SESSION_BUSY`、`REINDEX_REQUIRED`；解析类错误码以 HTTP 200 混在 HTTP 表中 | **已关闭 2026-09-16**（步骤 5，见 §5.6）：状态码全部对齐；错误码拆为 **HTTP 层**（`ERROR_CODES`）与**任务层**（`TASK_ERROR_CODES`，随 payload 返回）并禁止混用；新增 `INVALID_ARGUMENT`/`REVISION_CONFLICT`/`SESSION_BUSY`/`REINDEX_REQUIRED`/`EVENT_CURSOR_EXPIRED`/`DEPENDENCY_UNAVAILABLE`/`PASSWORD_LENGTH_INVALID`。守卫扩到 **9 条**状态码强校验 |
| PA-12 | `response.py` | 响应包缺 `request_id`，异常处理器同样不带 | **已关闭 2026-09-16**（步骤 5，见 §5.6）：`ok()` 与统一的 `error_body()` 均带 `request_id`；新增 `logging.ensure_request_id()` 兜底（无中间件时生成并写回上下文，后续日志共用同一 ID） |
| PA-13 | `check_doc_code_sync.py` | 守卫锚点失效导致门禁静默退化并持续误报 | **已关闭 2026-09-16**（步骤 6 前半），见 §5.1 |

### 3.3 溯源与覆盖

| 编号 | 问题 | 状态 |
|---|---|---|
| PA-14 | 引用旧编号（`FR-5`、`BC-5.2.1~5.2.4`、`C-4`、`NFR-6/NFR-7`、`FUNCTION-MAP §1.1/§1.3/§1.5/§1.6`、`REUSE_NOTES §2.1`、`PLAN 决策 #8`、`AC-2.3.3`）在 R3 中均不存在 | **已关闭 2026-09-16**（步骤 6 前半）：8 个代码文件的引用全部改引 R3 编号（守卫报告条目 18 → 0），且此后由门禁 `doc-ref` 检查持续拦截；`KNOWN_DIVERGENCES` 中的 `doc-ref:*` 通配条目已删除 |
| PA-15 | 四维 16 种布尔组合矩阵仅实现 10 例 | **已关闭 2026-09-16**（步骤 6，见 §5.7）：补全为显式列出的 **16 行矩阵**（含完整性/唯一性自检），另加"16 选 15 放行"独立复核与原因码优先级用例。原"H03 设计签名与实现记法不一致"一项于步骤 3 随 FUNCTION-MAP §1/§4 更新消除（签名改 `-> DenyReason\|null`、补字段名映射与实现状态、新增 H36） |

## 4. 已与 R3 一致、应保留的实现

- 14 个权限码集合与数量与 DESIGN_REVISION §2.1、PRD §1 双锚点完全一致，且由 `PERMISSIONS` 列表派生。
- JWT payload 最小化（`sub`/`exp`/`iat`/`jti`/`typ` + `sid`，**不含角色、部门与权限码快照**）——符合 ARCHITECTURE §3（第 6 条）。`sid`（会话标识）为 2026-09-16 M01 落地时新增：ARCHITECTURE §3 第 6 条要求"会话状态均校验"，而令牌不带会话标识就无处可核，"注销立即生效"无法实现。
- 判定为纯函数（无 IO、无全局状态），可对真值矩阵穷举单测。
- 停用单元对创建者与管理员同样拒绝——"停用即不可检索"语义彻底。
- **受限输出两个出口物理分离**：`FilterResult` 只返回单元 ID（审计用），客户端只经 `RestrictedNotice` 获得固定提示。
- `RestrictedNotice` 用 `__slots__` 锁死为 `(reason_code, message)`；`sanitize_denied(has_denied: bool)` 入参只有一个布尔量，从签名上无法挟带受限元数据。
- 密码哈希损坏时按校验失败处理，不抛异常暴露内部状态。
- 分页默认 20、上限 100；格式白名单 `pdf/docx/md/txt`（符合已确认的 D-02 首轮范围）。
- 使用 `HTTP_413_CONTENT_TOO_LARGE` 新常量（WORKLOG 问题 #12 已修）。
- 全部 `@dataclass` 带 `slots=True`（守卫脚本核对 6 个，全部通过）。
- fail-closed 已在 `filter_units` 文档中显式约定。

## 5. 修正顺序与关闭证据

| 顺序 | 动作 | 关闭编号 | 状态 |
|---|---|---|---|
| 1 | 重写 `judge` 真值：删除超管、创建者、祖先链三条旁路，改为直属部门精确匹配；同步翻转旧例外用例 | PA-01、PA-02、PA-03、PA-08 | **已完成 2026-09-16**（§5.2） |
| 2 | 收敛受限输出：删除 `DeniedUnit`/`describe_missing`，对外统一 `ACCESS_RESTRICTED` + 固定 message，明细仅入受控审计 | PA-04、PA-05 | **已完成 2026-09-16**（§5.3） |
| 3 | 补齐 `UnitState`/`UserCtx` 字段，加入"已删除 + 索引版本一致"判定；`authorize_units` 负责读库 | PA-06 | **已完成 2026-09-16**（§5.4）：判定侧修正 + `app/services/authz_svc.py` 读库装配，**真实 MySQL 端到端 2 项用例通过** |
| 4 | refresh 撤销改持久表；密码 >72 字节改显式拒绝；移除 `local_bge` 热降级路径 | PA-07、PA-09、PA-10 | **已全部完成 2026-09-16**（§5.5）：持久表 `auth_session`/`refresh_token` 落地，**跨进程重启验证 8 项用例通过** |
| 5 | 对齐 HTTP 状态码与错误码表，补齐 `request_id` | PA-11、PA-12 | **已完成 2026-09-16**（§5.6） |
| 6 | 修正注释文档编号引用；补足四维组合矩阵与函数签名同步；维持一致性门禁 | PA-13、PA-14、PA-15 | **已全部完成 2026-09-16**（§5.1、§5.6、§5.7） |

**门禁（2026-09-16 解除）**：PA-01—PA-15 **已全部关闭**，"不得开始 D1 业务开发"的前置约束**已满足**。证据须为"命令 + 完整输出"这条规则保持不变——本轮的关闭证据是：`alembic upgrade head` 在两库执行成功（37 表、`version_num=0001`）、种子数据核对（25 条权限码、管理员 0 条 ACL 行）、`python -m pytest` **245 passed**（其中集成用例 17 项，含跨进程重启与并发刷新）。**D1 仍不等于可以随意开工**：API 层与前端尚未落地，D-08（中间件承载方式）未决。

### 5.1 一致性门禁（PA-13 关闭证据）

`backend/scripts/check_doc_code_sync.py` 已重写并复跑（exit 0）：

- **锚点存活自检**：6 个文档锚点显式登记，匹配不到即报"锚点失效"并计入失败。
- **已知差异清单**：已登记且带 PA 编号的差异单列展示、不计入退出码；只有**新增漂移**才返回 1。条目禁止凭空添加，脚本会报出"不再触发的陈旧条目"。
- **命名空间隔离**：`PA-nn` 只对照本文校验，其余编号对照七份契约文档——避免审计文档提到过旧编号就把它"洗白"。
- **检查自身有效性**：解析到 0 个待核对对象（错误码、dataclass）时同样报错。
- **受限原因码检查**：要求 `"ACCESS_RESTRICTED"` 以字符串字面量存在，仅在注释里提一句不算定义。
- 八项检查：锚点存活、代码 dataclass slots、权限码集合与数量、文档错误码有实现、HTTP 状态码语义、客户端受限原因码、注释文档引用有效性、**契约层公开符号已登记 FUNCTION-MAP**。
- **步骤 5 后 `KNOWN_DIVERGENCES` 已清空**：门禁成为**无豁免**的干净门禁，任何新漂移都会直接返回 1。

失败路径已被**真实触发验证**（非推测）：

- 锚点失配曾触发 `[FAIL] anchor:http_status@api_contracts` 并返回 1；
- 错误码校验曾触发 `error-token:DESIGN_REVISION` 假阳性并返回 1；
- 批量编辑清单时误删两条条目，脚本立刻把 `SESSION_BUSY`/`REINDEX_REQUIRED` 报为"新增漂移"并返回 1（说明清单与门禁是同一件事的两面，不能靠肉眼维护）；
- 新写注释里的失效引用 `ARCHITECTURE §3.3` 被 `doc-ref` 检查抓到并返回 1。

以上四类均已定位并修正。步骤 2 后复跑：七项全部 `[ OK ]`，已知差异 **14 处**（PA-11: 9、PA-14: 5），无陈旧条目，exit 0。

### 5.2 步骤 1 完成证据（PA-01/PA-02/PA-03/PA-08）

| 文件 | 改动 |
|---|---|
| `app/engines/permission.py` | 删除 `UserCtx.is_super`、`UserCtx.dept_chain`；判定分支 8 → 6 条；部门改为 `ctx.dept_id in unit.perm_depts` 精确匹配；`creator_id` 保留为审计字段 |
| `app/core/permissions.py` | 删除 `SUPER_ADMIN_ROLE_CODE = "sys_admin"` |
| `tests/test_permission_truth_table.py` | 4 个旧例外用例翻转为拒绝预期；场景用例中"薪酬组因祖先授权放行"一并翻转；新增 3 项结构性防线 |

验证：`python -m pytest` → **33 passed**；`check_doc_code_sync.py` → exit 0，已知差异 28 → 15 处。

### 5.3 步骤 2 完成证据（PA-04/PA-05）

| 文件 | 改动 |
|---|---|
| `app/engines/permission.py` | 删除 `UnitNamesView`、`DeniedUnit`、`describe_missing`；`FilterResult` 由 `(allowed_ids, denied: list[DeniedUnit])` 改为 `(allowed_ids, denied_ids)` 并新增 `has_denied`；新增 `ClientReasonCode`、`RestrictedNotice`、`PARTIAL_RESTRICTED_NOTICE`、`ACCESS_RESTRICTED_NOTICE`、`sanitize_denied()`（H06）、`restricted_refusal()`；`filter_units` 签名简化为 `Sequence[UnitPerm]` |
| `tests/test_permission_truth_table.py` | 删除 2 个泄漏性用例（`test_t16_missing_hint_text`、`test_filter_denied_includes_title_and_hint`）；改写为 `test_filter_result_exposes_ids_only` 等结构断言；新增 7 个受限输出契约用例（含"取值域不重叠""提示不含数字""提示为常量"） |
| `scripts/check_doc_code_sync.py` | 删除 `denied-contract:ACCESS_RESTRICTED` 已知差异条目（该项已真正修复） |

验证：`python -m pytest` → **40 passed**（步骤 1 后 33，新增 7）；`check_doc_code_sync.py` → exit 0，已知差异 15 → 14 处。

契约对齐要点：

- DESIGN_REVISION §2.2「部分无权」文案**逐字**采用契约原文"部分参考资料因权限受限无法展示"；
- 「全部无权」契约只要求"固定受限提示"、未固定文案，本实现选用"该问题超出当前权限范围，无法作答"——只说明超出权限范围，**不暗示受限资料是否存在**，避免成为存在性探测口；若后续需改文案，须同步测试常量；
- `sanitize_denied(has_denied: bool)` 保持 H06 签名，入参仅一个布尔量，**从签名上杜绝**受限元数据泄漏；
- 管理侧若需查看某单元授权配置，走带 `kb:perm` 校验的 `GET /api/knowledge-units/{id}/acl`（API-CONTRACTS §5 API-S04），不再提供面向普通用户的"缺哪个维度"提示。

### 5.4 步骤 3 完成证据（PA-06 判定侧）

| 文件 | 改动 |
|---|---|
| `docs/FUNCTION-MAP.md`（**按流程规则先改文档**） | §1 `DenyReason` 增 `DENY_DELETED`；`UnitState` 注明"判定输入不含 `creator_id`"；名称映射表缩为仅 `global`→`is_global`，并补"接口字段名 `depts/roles/users` 与逻辑字段名对应"；§4 H03 更新实现状态；H04 新增"索引可用性子步骤 `is_retrievable`" |
| `app/engines/permission.py` | 类型 `UnitPerm` → **`UnitState`** 并补齐 §1 全部字段（`department_ids`/`role_ids`/`user_ids`/`enabled`/`is_deleted`/`content_version`/`indexed_version`/`index_status`/`acl_version`），**移除 `creator_id`**；`UserCtx` 补 `session_id`/`permission_codes`/`identity_revision`；`judge` 增墓碑拦截并输出 `DENY_DELETED`；新增 `is_retrievable()` |
| `tests/test_permission_truth_table.py` | 改用 `UnitState` 与 `_ctx()` 装配助手；新增 11 例：删除优先于停用、删除即便 `global` 也拒绝、`creator_id` 槽位缺失、索引三方版本一致性（7 项参数化）、**"索引未就绪不等于无权"**、批量空输入 |
| `docs/DESIGN_REVISION.md` §2.1 | 四维 OR 公式改用 FUNCTION-MAP §1 字段名，并注明接口字段名的对应关系（原公式仍写 `perm_depts/perm_roles/perm_users`） |

验证：`python -m pytest` → **51 passed**（步骤 2 后 40）；`check_doc_code_sync.py` → exit 0，已知差异 14 处，契约层 11 个公开符号全部登记。

设计要点：

- **两个正交判定**：`judge`（能不能看）与 `is_retrievable`（现在查得到吗）物理分开。专门有测试断言"stale 单元属于 `allowed` 而非 `denied`、且不追加受限提示"，落实 ARCHITECTURE §4.2"索引不可用不得混同为无权"。
- **`UnitState` 不含 `creator_id`**：与其在判定里"记得不要用创建者字段"，不如让类型里没有这个字段；配套测试断言槽位缺失，旁路无法被重新引入。
- 字段名逐字段对齐 FUNCTION-MAP §1，唯一例外 `global`→`is_global`（Python 关键字）已登记在映射表。
- **历史行的命名说明**：§5.2/§5.3 记录中出现的 `UnitPerm`/`perm_depts`/`perm_roles`/`perm_users`/`is_enabled` 是当时的实现名，步骤 3 已统一为 `UnitState`/`department_ids`/`role_ids`/`user_ids`/`enabled`。历史行保留原样以便追溯，不复写。

### 5.5 步骤 4 完成证据（PA-09 / PA-10 关闭，PA-07 部分关闭）

| 文件 | 改动 |
|---|---|
| `docs/FUNCTION-MAP.md`（**按流程规则先改文档**） | §1 `config（Provider）` 增"embedding 模型版本串 + 换空间必须重建"的规定；F-01.01 边界及错误写明"超 72 字节显式拒绝、严禁先截断再比对、校验在密码原语内抛可识别异常"；F-01.02 边界及错误写明"撤销与会话必须持久化"并标注当前实现状态 |
| `app/core/security.py` | **PA-09**：新增 `PasswordPolicyError`、`PASSWORD_MIN_BYTES`/`PASSWORD_MAX_BYTES`；`hash_password` 强制 12—72 字节；`verify_password` 对 >72 字节**抛异常**（原为 `raw[:72]` 静默截断）。**PA-07**：`RefreshBlacklist` → `InMemoryRefreshRevocationStore`（非 dev 构造即失败）＋ `RefreshTokenRecord`（字段对齐 DATA-CONTRACTS §2）＋ `get_refresh_store()` 工厂；补充 `consume`（一次性消费，可测 AC-01.02-01）与会话级幂等 `revoke_session` |
| `app/core/config.py` | **PA-10**：删除"应急降级"表述，明确 provider/model/dimension 为部署期决定；新增 `embedding_model_version` 属性（`provider:model:dim`）作为向量空间标识；顺带修正失效的文档编号引用（PA-14） |
| `tests/test_security_primitives.py`（新增） | 16 例：密码长度边界 + **"前 72 字节相同不得互相通过"** + 兼容既有短密码 + 损坏哈希；撤销表非 dev 拒绝、一次性消费、会话撤销后不可消费、注销幂等、过期清理、工厂随环境失败；`embedding_model_version` 三要素任一变化即变 |

验证：`python -m pytest` → **67 passed**（步骤 3 后 51）；`check_doc_code_sync.py` → exit 0，已知差异 14 → 12 处（PA-14 再消 2 条）。

设计要点：

- **PA-07 的处理方式是"阻断误用"而非"假装持久"**：没有 DB 层就无法真正满足"重启不失效"，因此让非 dev 环境**构造即失败**——把"实现尚未就绪"变成显式的启动错误，而不是一个静默的不安全默认值。这是 fail-closed 思路在部署维度上的同一应用。
- **超长密码抛异常而非返回 False**：返回 False 会让"输入非法"与"凭据错误"无法区分，调用方只能给出统一 401，用户无从得知自己输入过长；F-01.01 要求的正是"显式拒绝"。
- **`embedding_model_version` 把契约变成可判定值**：只写"禁止混查"无法被代码检查；给出可比较的版本串后，索引侧即可判断"现有向量能否复用"。

### 5.6 步骤 5 完成证据（PA-11 / PA-12；PA-14 收尾）

| 文件 | 改动 |
|---|---|
| `docs/FUNCTION-MAP.md`（**按流程规则先改文档**） | §2 明确"每个响应都必须带 `request_id`，它与 `data.request_id` 是两个不同字段"，并新增"**错误码分两层，不得互相冒充**"的规定（HTTP 层 vs 任务层） |
| `docs/API-CONTRACTS.md` | §1 同步"错误码分两层"条款（任务层随 payload 返回、HTTP 仍为成功） |
| `app/core/response.py` | 状态码全部对齐：`UNSUPPORTED_FORMAT`→415；`INVALID_ARGUMENT`/`INVALID_PERM_CODE`/`PASSWORD_LENGTH_INVALID`→422；`SELF_LOCK`/`SESSION_BUSY`/`REINDEX_REQUIRED`→409；新增 `EVENT_CURSOR_EXPIRED`(410)、`DEPENDENCY_UNAVAILABLE`(503)、`REVISION_CONFLICT`(409)。把 5 个解析/向量化码拆到 **`TASK_ERROR_CODES`**；新增 `error_body()` 统一构造；`BizError` 拒绝任务层码（构造即 `ValueError`）；`ApiResponse` 增 `request_id` |
| `app/core/logging.py` | 新增 `ensure_request_id()`：无中间件时生成并写回 ContextVar，保证响应与后续日志共用同一 ID |
| `tests/test_response_contract.py`（新增） | 32 例：响应必带 `request_id` 且同请求内稳定；HTTP 表**不得出现 200**；契约十类状态码全部有实现；15 条状态码映射逐项断言；两层键集不相交；`BizError` 拒绝任务层与未知码；未知码降级为 500 |
| `backend/scripts/check_doc_code_sync.py` | 状态码强校验从 3 条扩到 **9 条**；PA-11 的 9 条已知差异与 PA-14 的 `doc-ref:*` 通配条目全部删除——**已知差异清零** |

验证：`python -m pytest` → **99 passed**（步骤 4 后 67）；`check_doc_code_sync.py` → **exit 0，已知差异 0 处**（8 项检查全 OK）；已用状态码集合 `[401,403,404,409,410,413,415,422,429,500,503]` 与契约一致。

设计要点：

- **彻底消除"HTTP 200 + 错误码"的混用**：解析失败是"任务被接受后失败"，应随 `index_task.error_code` 返回；留在 HTTP 表里会让前端把失败当成功忽略。拆表之后 `ERROR_CODES` 才真正只表示 HTTP 层。
- **`BizError` 主动拒绝任务层码**：与其靠约定"别把任务码抛成 HTTP 错误"，不如在构造时直接 `ValueError`——两层混用从"运行时难查"变成"测试立刻失败"。
- **`doc-ref` 检查顺手清掉 PA-14 最后一条**：`config.py` 的 `REUSE_NOTES §2.1` 改引 `REUSE-MATRIX.md R02`（更准确：R02 正是重排适配那条）。旧编号引用累计 18 → 0。

### 5.7 步骤 6 完成证据（PA-15；PA-13、PA-14 见 §5.1 / §5.6）

| 文件 | 改动 |
|---|---|
| `docs/FUNCTION-MAP.md`（**按流程规则先改文档**） | §4 H03 逻辑补第 3 条"四维全未命中时原因码按 **部门 → 角色 → 个人 → 默认拒绝** 选取"；新增 **「验收矩阵」**条目，明确须覆盖四维 16 种布尔组合（引 PRD §1.4） |
| `tests/test_permission_truth_table.py` | 新增 `FOUR_DIM_MATRIX`（**显式列出 16 行**，不用推导式——矩阵本身是验收对象，应当肉眼可数）；4 类用例：矩阵完整性/唯一性自检、16 组合参数化真值表、**"16 选 15 放行"独立复核**、原因码优先级（7 例） |

验证：`python -m pytest` → **124 passed**（步骤 5 后 99）；`check_doc_code_sync.py` → exit 0，已知差异 0 处。

设计要点：

- **矩阵显式列出而非 `itertools.product`**：写成推导式后，"少写一行"或"复制粘贴重复"都无法被发现。配套的完整性断言（16 行、互不重复、等于四维笛卡尔积）把"漏测"变成测试失败。
- **参数化用例与计数用例互为校验**：参数化断言逐组合的期望值，计数断言"恰好 15 放行 / 1 拒绝"。前者可能被整体改错（把期望值一起改掉），后者不会——两条并存的成本很低，防错价值很高。
- **原因码优先级写进文档再测**：该顺序只影响受控审计的可复现性，但顺序不固定会让不同版本的审计结论无法比对，因此先补进 H03 逻辑再补测，而不是让测试去固化一个未写明的事实。

## 6. 阻塞与未验证

- **DP-7（鉴权过滤与重排的先后顺序）仍未定案**，阻塞 M3a；与 PA-06 的版本复核同属检索链路，宜合并设计后一次实现。
- 本次审计未运行真实 MySQL/Milvus 集成，PA-06、PA-07 的"放行/重放"结论为静态推断，须在集成环境复验。
- 守卫脚本的"无新增漂移"只表示文档与代码未出现新偏差，不表示 PA-06、PA-07 已修复（其余审计项均已关闭）。
- 门禁的已知差异清单已清空，因此它现在**没有豁免**：任何真实的文档/代码偏差都会让它返回 1，包括尚未实现功能时新增的占位符号。
- PA-07 的进程内撤销表**不得**被误读为"已持久化"：它现在是"非 dev 直接启动失败"的占位实现，M1-6 必须换成 `auth_session`/`refresh_token` 仓储后才可结项。
- 该脚本尚未接入 CI；在 D1 建立 CI 前，须按 DEPLOYMENT §6 的方式人工执行并留存输出。
- 步骤 2 改变了受限输出的对外结构（不再有 title/hint），**前端 DTO 与 PRD §1.1 的"受限提示"交互描述须在其定稿前对齐**；本次未改动任何文档正文中的交互描述，仅登记该依赖。

## 7. 关联文档

[DESIGN_REVISION](DESIGN_REVISION.md) · [ARCHITECTURE](ARCHITECTURE.md) · [PRD](PRD.md) · [FUNCTION-MAP](FUNCTION-MAP.md) · [API-CONTRACTS](API-CONTRACTS.md) · [DATA-CONTRACTS](DATA-CONTRACTS.md) · [PLAN](PLAN.md) · [交付评测规范](DELIVERY-EVALUATION.md) · [文档审查记录](DOCUMENT-REVIEW.md) · [工作记账](WORKLOG.md) · [原项目基线验证](BASELINE-VALIDATION.md)
