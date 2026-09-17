# 前端规格（FRONTEND-SPEC）· 知识库管理平台

版本：2026-09-16 R3。**设计稿，非实现完成声明。** 本文把 [PRD](PRD.md) §1.1 的"七个页面/面板"展开为可实现的路由表、页面结构、交互逻辑与视觉规范；不新增产品模块、不新增 US/F/H 编号。

权威顺序：用户明确要求 → [DESIGN_REVISION](DESIGN_REVISION.md) 安全契约 → [PRD](PRD.md) / [FUNCTION-MAP](FUNCTION-MAP.md) → [API-CONTRACTS](API-CONTRACTS.md) / [DATA-CONTRACTS](DATA-CONTRACTS.md) → 本文。本文只定义**表现层**，不得据此放宽任何权限或安全语义；与上游冲突时以上游为准。

配套：[API 与运行协议契约](API-CONTRACTS.md)、[格式与验收](FORMAT-ACCEPTANCE.md)、[复用与改造矩阵](REUSE-MATRIX.md)、[图册](diagrams/README.md)。

## 1. 技术栈与工程边界

| 项 | 选择 | 依据 / 说明 |
|---|---|---|
| 框架 | Vue 3 + Vite + TypeScript | PRD §0 第 3 条"保留 FastAPI/Vue/MySQL/Milvus 分层单体"；FUNCTION-MAP §0 第 2 条"前端 TypeScript" |
| 路由 / 状态 | vue-router 4 + Pinia | 与既有参考工程一致，降低迁移成本 |
| UI 组件库 | Element Plus | archive 早期规划栈即为 Element Plus；**参考工程未使用任何 UI 库**，故组件层属新增而非复用 |
| 图表 | ECharts 5 | M08 六类图表；参考工程无图表库（仅自绘 SVG Sparkline），属新增 |
| Markdown | markdown-it + DOMPurify + highlight.js | F-10.02 要求"禁原始 HTML 与危险协议；文本代码高亮复制"，参考工程按纯文本渲染，**不可复用** |
| HTTP | 原生 `fetch`（上传用 `XMLHttpRequest`） | 与 H30/上传进度契约一致；不引入 axios |
| 样式 | Element Plus 主题变量覆盖 + 自有设计令牌 CSS 变量 | 见 §7 |
| 工程位置 | `frontend/`（独立工程，与后端解耦） | 开发态经 Vite dev server 代理 `/api` 到后端；生产托管方式见 §10 |

**不复用项（有据可查）**：`REUSE-MATRIX.md` R11 判定参考工程前端"自报角色头（`X-Role`/`X-Tenant-Id`）"不可作为身份事实源；`REUSE-MATRIX.md` §3 第 4 条判定其 SSE 解析"无 `id`/`seq`、坏 JSON 静默 `continue`"，**不满足 R3 事件重放**。因此鉴权层与流解析层为**重写**，参考工程仅作页面结构与交互形态的参考。

## 2. 功能模块 → 页面 → 路由映射

PRD §1.1 给出七个页面/面板；M01（身份认证）由登录页承载，M02/M03/M04/M06/M07/M08/M09 承载余下六个页面；M10 是横向能力（路由门禁 + 流式渲染），不单独成页。

| 模块 | 模块名（PRD §2） | 页面 | 路由 | 入口权限码 |
|---|---|---|---|---|
| M01 | 身份认证 | 登录页 | `/login` | 公开（API-CONTRACTS §2：login/refresh 不前置 H02） |
| M02 | 组织用户与功能权限 | 组织配置页 | `/org` | `sys:dept` / `sys:user` / `sys:role`（**三面板独立授权**） |
| M03 | 文档导入解析与任务 | 知识中心·导入抽屉 | `/knowledge`（抽屉） | `kb:upload` |
| M04 | 知识生命周期与四维权限 | 知识中心 | `/knowledge` | `kb:view`（正文/切片另需数据读权；`kb:edit`/`kb:delete`/`kb:perm` 控制对应操作） |
| M05 | 会话检索与流式问答 | 问答工作台 | `/chat` | `ai:ask` |
| M06 | FAQ 沉淀审核与缓存 | 沉淀运营页·FAQ 页签 | `/precipitation` | `faq:review` / `faq:publish`（按操作区分） |
| M07 | 知识缺口闭环 | 沉淀运营页·缺口页签 | `/precipitation` | `gap:handle` |
| M08 | 审计与数据看板 | 运营看板 | `/dashboard` | `dashboard:view` |
| M09 | 模型配置与运行控制 | 模型配置面板 | `/model-config` | `sys:model` |
| M10 | 前端交互与交付验收 | —（横向） | — | — |
| — | 无权限 / 未匹配 | 403 / 404 | `/403`、`/:pathMatch(.*)*` | — |

**路由层级与默认落点**

- 已登录后 `/` 重定向到**当前身份可见的第一个菜单项**（顺序：`/chat` → `/knowledge` → `/precipitation` → `/dashboard` → `/org` → `/model-config`）；全部不可见时落到 `/403`。
- 菜单分组：**问答**（问答工作台）、**知识**（知识中心）、**沉淀**（沉淀运营）、**系统**（运营看板、组织配置、模型配置）。
- 具体 `path` 字符串上游未规定（FUNCTION-MAP §3-M10 F-10.01 只输出 `{path,label}`），由本文定义，属实现约定。

## 3. 路由门禁与按钮门禁（F-10.01）

- **菜单解析**：`resolve_navigation(identity, routes)` 的前端实现为纯函数——路由表静态声明 `meta.permissions`，按 `identity.permission_codes` 过滤输出 `items:[{path,label}]`。
  - `meta.permissions: string[]` 语义为**任一命中即可见**；`meta.permissionsAll: string[]` 语义为**全部命中才可见**（本项目仅 `/org` 用到"任一"语义，其余为单码）。
- **全局前置守卫**：无身份 → `/login`；有身份但目标路由权限不足 → `/403`。
- **401 处理**：任意响应 401 → 清空身份 → `/login`；`403` → 页面内提示 + 保留当前位置（不跳转），符合 PRD §1.4 界面验收"无权按钮直接接口调用"仍需后端拒绝。
- **按钮门禁**：`v-permission` 自定义指令控制显隐，权限码取自 `GET /api/auth/me`（F-01.04）的 `permission_codes`。
- **不可放宽**：AC-10.01-01「直接 URL 访问受控，后端仍做授权」；AC-10.01-02「刷新身份时同步菜单，**不以 UI 代替安全边界**」。前端门禁是**可用性**措施，不是授权事实来源（DESIGN_REVISION §2.1"菜单和按钮不代替后端检查"）。
- **身份刷新**：登录后、以及每次路由切换时按需重取 `/api/auth/me` 同步 `permission_codes`；身份 `revision` 变化即重建菜单（`identity_revision`，FUNCTION-MAP §1 `UserCtx`）。

## 4. 页面结构

### 4.1 登录页 `/login`（M01）

- **布局**：整屏居中卡片；左侧品牌区（产品名 + 一句话定位），右侧表单区；窄屏时品牌区收起。
- **元素**：用户名输入、密码输入（可见性切换）、提交按钮、行内错误区、加载态。
- **字段约束**（对齐 API-CONTRACTS §1）：`username` 3—64 字符；密码**不 trim 不截断**，登录允许旧短密码但拒绝 >72 字节。
- **交互**：提交 → `POST /api/auth/login`（F-01.01）。
  - 成功：`access_token` / `refresh_token` **只存内存**（Pinia），`expires_in` 用于被动续期；随后请求 `/api/auth/me` 取得权限码并按 §3 落点。
  - 401：固定文案，不区分"用户不存在/密码错误"（防枚举）。
  - 429：提示限流并按 `Retry-After`（若提供）禁用提交。
  - 5xx / 网络失败：可重试，保留已填用户名（PRD §1.1.1"刷新令牌失败回登录，**不丢弃非敏感草稿**"——仅保留 `username`，密码永不留存）。
- **退出**：`POST /api/auth/logout`（F-01.03）+ 本地清身份 → `/login`。注销后 access token 立即失效依赖服务端 `sid` 校验（ARCHITECTURE §3 第 6 条），前端不做假设式放行。

### 4.2 组织配置页 `/org`（M02）

三面板布局，**各自独立按权限码渲染**（PRD §1.1.2）：左「部门树」需 `sys:dept`，中「用户」需 `sys:user`，右「角色」需 `sys:role`；无权限的面板整块不渲染（而非渲染后禁用）。

**部门树面板**

- 数据：`GET /api/departments`（F-02.01）→ `{items:[{id,parent_id,name,revision}]}`，扁平列表在前端组树；部门树是 API-CONTRACTS §1 分页规则的**明确例外**。
- 操作：新建（F-02.02）、编辑/移动（F-02.03，`parent_id` 可空表示根）、删除（F-02.04）。
- 前置校验：**部门不能移动到自身后代**（DESIGN_REVISION §7）；前端做树内预检，后端仍强制。
- 删除遇引用返回 **409**，提示"先迁移成员或撤销引用"（DESIGN_REVISION §7）。
- 所有写操作携带 `expected_revision`。

**用户面板**

- 列表：`API-S01 GET /users`（`q,page,size,enabled?`）→ `items:[{id,username,dept_id,role_ids,enabled,revision}]`；**不返回密码哈希**。
- 列：用户名 / 直属部门 / 角色（多标签）/ 启用状态 / 操作。
- 新建（F-02.05）：`username`、`password`（创建时 12—72 UTF-8 字节）、`dept_id`、`role_ids`。
- 编辑启停（F-02.06）：`dept_id`、`role_ids`、`enabled`、`expected_revision`；**停用账号对所有人（含创建者与管理员）不可检索**（安全契约），前端在停用确认框显式提示该后果。

**角色面板**

- 列表：`API-S02 GET /roles` → `items:[{id,name,codes,revision}]`。
- 功能树：`API-S03 GET /permission-codes` → `items:[{code,label,module}]`，**由后端返回、前端组树**（API-CONTRACTS §5）。上游未枚举 `module` 取值，前端按返回的 `module` 动态分组，不硬编码映射表。
- 保存：`PUT /api/roles`（F-02.07，`id=null` 为新建）、删除 `DELETE /api/roles/{id}`（F-02.08）。
- **默认权限提示**：DESIGN_REVISION §2.1 只规定"系统管理员默认管理组织、功能授权、配置和看板"，未规定其余角色的默认码清单——因此**新建角色默认不勾选任何权限码**，由管理员显式分配，不以任何硬编码默认值代替。

**目录选择器**：`GET /api/directory?kind=user|role|department`（F-02.09）供选择器使用；完整用户列表用 API-S01。

### 4.3 知识中心 `/knowledge`（M03 / M04）

**工具栏**：`q`（≤200 字符）、分类筛选（≤100 字符）、启用状态、分页（默认 `page=1,size=20`，上限 100）；「导入」按钮需 `kb:upload`。

**台账表**（F-04.01）列：编码 `code` / 标题 / 格式 / 分类 / ACL 标签 / 更新时间 / 启用 / 索引状态 / 操作。

- **"列表返回后保留筛选和页码"**（PRD §1.1.3）：筛选条件同步到 URL query，从详情返回时按 query 复原，不重置到第一页。
- 索引状态列按 §5.3 的状态映射着色。

**导入抽屉**

1. **选择**：支持文件多选与**文件夹拖拽**；`H32 expand_drop` 递归枚举目录、保留 `relative_path` 仅作展示与分组、做清单预校验（拒绝绝对路径/盘符/`..`/NUL，API-CONTRACTS §3）。
2. **清单表**：相对路径 / 大小 / 分类 / 状态（待上传·上传中·已接受·失败）。前端并发上限 **3**（API-CONTRACTS §3）。
3. **提交**：单文件 `POST /api/uploads`（F-03.01，含 `client_upload_id`）；批量 `POST /api/upload-batches`（F-03.02，`files` 与 `manifest.items` **长度与顺序严格匹配**）。
4. **两类进度必须分开显示**（FUNCTION-MAP §2.4 明令）：传输进度来自 `XMLHttpRequest.upload.onprogress`；解析/索引进度来自 `GET /api/index-tasks/{task_id}`（F-03.03）轮询。**前端不得把"传输完成"当作"索引完成"**。
5. **部分失败可单独重试**（DESIGN_REVISION §8）：批量响应 `items[].error_code` 定位到具体文件；重试走 F-03.05（`expected_revision`）。

**详情抽屉**

- 元数据编辑（F-04.02）：标题（≤200）、分类（≤100）、`expected_revision`。
- 正文切片（F-04.03）：`{chunk_id,version,seq,text,page_no,offset}`，分页展示；`page_no`/`offset` 为空时显示页级/段级定位，**不伪造精确位置**（FORMAT-ACCEPTANCE §1）。
- 切片编辑/拆分/删除（F-04.05）：`action` + `text`/`split_offset`；提交后返回 `{task_id,target_version}` → 转入索引任务轮询。
- 替换文档（F-04.04）、启停（F-04.06）、删除（F-04.07，返回 `{deletion_id,cleanup_status}`）。

**四维权限弹窗（H33 `normalize_acl`）**

- 回填：`API-S04 GET /knowledge-units/{id}/acl` → `{global,depts,roles,users,revision}`。
- 交互：`global` 开关 + 部门/角色/用户多选（实体取自 `GET /api/acl-entities`，F-04.09）；**关闭 `global` 保留其他选择**；实体去重。
- **全空时必须提示"四维全空则无人可读（含创建者与管理员）"**，提交后仍由 F-04.08 后端复核（FUNCTION-MAP §2.4）。
- 提交：`PUT /api/knowledge-units/{id}/acl` → `{acl_version,revision}`；提示"权限变更即时生效，已发送的流内容无法撤回"（DESIGN_REVISION §2.3）。
- `kb:perm` 是敏感能力，弹窗内需二次确认。

### 4.4 问答工作台 `/chat`（M05）

**三区布局**：左「会话侧栏」/ 中「对话区」/ 右「来源与引用面板」（可折叠）。

**会话侧栏**：`GET /api/sessions`（H28）分页；新建（F-05.01）、重命名与删除（F-05.03）。侧栏条目含 `updated_at` 稳定排序；**不展示他人会话标题**。

**对话区**

- 输入：`textarea`（问题 1—4000 字符）+ 智能联想（F-05.09，前缀输入防抖 300ms，`limit` 受控）+ 发送 / 停止。
- 发送：`POST /api/chat/requests`（F-05.04，body `{session_id,client_request_id,question}`）。
  - **`client_request_id` 由前端生成并在重试中复用同一值**（UUID），保证幂等：同键同载荷返回 200 与原 `request_id`，同键异载荷 409。
  - 同一会话首版只允许一个活动请求，竞争返回 **409 `SESSION_BUSY`** → 提示"本会话有进行中的提问"。
- 订阅：`GET /api/chat/requests/{id}/events?after_seq=N`（F-05.06，H31 解析）。**`EventSource` 不支持自定义头，故用 `fetch` 读取流以携带 `Authorization`，绝不把 token 写入查询参数**（API-CONTRACTS §2）。
- **恢复状态与生成状态分开显示**（PRD §1.1.4）：恢复态（重连中 / 已恢复快照）与生成态（生成中 / 已完成 / 已取消 / 失败）是两个独立指示，不合并成一个 loading。

**事件处理**（API-CONTRACTS §4）

| 事件 | 前端行为 |
|---|---|
| `meta` | 记录 `request_id`/`session_id`/`status`，状态置"进行中" |
| `delta` | 追加文本（**先完整解帧并校验 `seq`，再应用**） |
| `citations` | 渲染引用卡（`no`/`title`；`unit_id`/`version`/`chunk_id` 不直接展示为敏感信息） |
| `denied` | 渲染**固定受限提示**，且**不得含受限 ID/标题/摘要/部门名单/数量** |
| `done` | 置终态、展示 `result_type`、`usage`、`duration_ms`、`first_token_ms` |
| `error` | 安全错误文案 + 可重试；**不据其推进持久 `seq`** |
| `heartbeat` | 注释帧，**不持久化、不推进游标** |

**游标与幂等**：`id=seq` 每请求内递增；**完整解帧 → 校验 `seq` → 按幂等规则应用文本/引用/状态 → 提交已应用的 `last_seq`**；**不得先保存游标再处理内容**；缺序重新订阅，**不跳过坏帧**。累计回答与游标只保留在内存，避免撤权后从本地缓存恢复敏感答案。

**重连**：保留 `after_seq` 重订阅（兼容 `Last-Event-ID`，两者同供必须一致）；`N > last_seq` 返回 422；**保留期外 410 → 取安全快照** `GET /api/chat/requests/{id}`（H27），**不得重新 POST 同问题产生新请求**（否则重复计 PV，Q01 验收）。

**取消**：停止按钮 → `POST /api/chat/requests/{id}/cancel`（F-05.07）；**已完成态不改为取消；断网不等于主动取消**。

**组件销毁**：中止订阅但**不自动取消服务端请求**（FUNCTION-MAP §2.4）。

**引用卡（H35）**：点击 → `GET /api/chat/requests/{id}/citations/{no}`（F-05.08）→ 展示 `title`/`snippet`/`page_no`/`offset`；来源已在服务端复核，失效时显示"不可用"占位。

**流式 Markdown（F-10.02 `render_answer`）**

1. 事件序号去重；
2. `requestAnimationFrame` 批次输出（节流渲染，避免逐 token 重排）；
3. **禁原始 HTML 与危险协议**：`markdown-it` 关闭 `html`，输出经 `DOMPurify` 净化，链接协议白名单 `http/https/mailto`，其他协议不渲染为可点击链接；
4. 代码块按文本渲染 + 语法高亮 + 一键复制（复制内容与原文一致，AC-10.02-01）；
5. 引用角标可点击定位到引用卡；
6. **末批必须 flush**（`done`/`error` 前清空 rAF 队列），跨 UTF-8 分块不截断多字节字符。

### 4.5 沉淀运营页 `/precipitation`（M06 / M07）

两个顶层页签，按权限码独立渲染：FAQ 页签需 `faq:review` 或 `faq:publish`；缺口页签需 `gap:handle`。

**FAQ 页签**

- 子页签「候选 / 已发布」，数据 `GET /api/faqs`（H34）：`{id,question,answer,status,frequency,confidence,source_refs,hit_count}`。
- **多来源展示必须逐个列出**：多来源 FAQ 需**每一个来源当前都可读**（来源之间 AND，DESIGN_REVISION §5）——前端逐条渲染来源并标注各自状态，不做合并展示。
- 增量挖掘：`POST /api/mining/runs`（202，含 `client_action_id`）→ 轮询 `API-S07 GET /mining/runs/{id}` 展示 `status/consumed/candidates/failed/error_code`；`faq:review` 权限。
- 候选编辑（F-06.02）：问题（1—4000）/ 答案（1—20000）/ 来源（`source_ids`，去重后 ≤1000）。
- 发布（F-06.03）/ 状态流转（F-06.04 `action=reject|offline|resubmit`，`reason` 1—1000）/ 缓存开关（F-06.05）。
- **状态机可见**（PRD §1.2.4）：`candidate → published | rejected`；`published → offline`；来源失效 → `stale`；`offline`/`stale` 重新审核前转 `candidate`。**"编辑已发布答案先下线再重新审核"**，前端对 `published` 条目的编辑按钮改为"先下线"引导。
- **`stale` 必须显式提示**："来源正文已变更或删除，需重新审核后发布"，且清缓存。

**缺口页签**

- 列表（F-07.02）：`{id,question,dept_id,recent_frequency,max_similarity,suggested_category,last_seen_at,status}`。
- 转建（F-07.03）：`client_action_id` 幂等，**重复点击只建一个任务**。
- 补充任务：`API-S05 GET /supplement-tasks`（`gap_id?`）；绑定文档 `API-S06 PUT /supplement-tasks/{id}/source`（`unit_id` + `expected_revision`，**禁止绑定不存在/已删除单元**）。
- 回放验证（F-07.04）→ `{passed,state,reason}`；**回放失败保持 `processing`**，**仅上传完成不自动关闭**（DESIGN_REVISION §5）。
- 状态可见：`open → processing → closed`。关联补充任务是**独立实体**，不得用 `knowledge.index_status=processing` 表示（PRD §1.2.5）。

### 4.6 运营看板 `/dashboard`（M08）

- **筛选**：`range=day|week` + `anchor_date`（ISO 日期）；时间按 **Asia/Shanghai** 自然日/ISO 周。
- **摘要卡**（F-08.02）：`pv`、`uv`、`faq_hit_rate`、`coverage`、`knowledge_count`、`error_rate`、`unknown_usage_count`。
  - **零分母显示 0 并标注"无样本"**；**空样本为 `null` 显示"—"**，不得显示为 0。
  - 每张卡带口径说明（tooltip）：PV/UV/命中率/覆盖率的精确定义见 PRD §1.3 第 1—2 条。
  - **覆盖率 ≠ 召回率**，"纯检索召回率另列"（PRD §1.3 第 2 条），两者不合并展示。
- **六类图表**（F-08.03，`top_n` 1—50）：
  | 图表 | 数据项 | 呈现 |
  |---|---|---|
  | traffic | `date/pv/uv` | 双线折线 |
  | questions | `key/label/count` | 横向条形 |
  | knowledge_heat | `key/label/count` | 横向条形 |
  | usage | `date/生成tokens/embedding_tokens/rerank_units/unknown_count` | 堆叠柱 + 未知单列 |
  | latency | `result_type/lower_ms/upper_ms/count` | 分桶柱 |
  | knowledge_counts | 未删除/启用/已索引 | 环形 |
- **口径红线**：图表数据服务端已按上海日/周分桶，**前端不二次按本机时区重新聚合**；**访问失败时不把上次缓存图冒充当前范围结果**（FUNCTION-MAP §2.4）——失败即显示错误态与重试，不清空成 0 也不回落到旧数据。
- **脱敏**：问题排行只显示调用者本人问题；跨用户只给脱敏主题/计数；知识标题仅调用者有读权时显示，否则统一"受限知识"并合并计数，**不传受限 ID**（API-CONTRACTS §6）。
- **审计页签**（F-08.04）：`request_id`/`action` 筛选 + 分页 + 脱敏展示 `search_audit` 项。

### 4.7 模型配置面板 `/model-config`（M09）

- **四个分区**：模型（`llm_model` / `embedding_model` / `rerank_model`）、阈值（`faq_threshold` / `cluster_threshold` / `gap_threshold`，范围 `[0,1]`）、频次（`mining_interval_seconds` ≥ 60）、限额（`top_k`，范围 `[1,100]`）。
- 读取：`GET /api/model-config`（F-09.01）→ `{revision,models,thresholds,limits,key_configured}`。
- 保存：`PATCH /api/model-config`（F-09.02）**仅提交变更字段** + `expected_revision`；**patch 白名单之外字段一律 422**，前端不提供额外可编辑项。
- 探测：`POST /api/model-config/probe`（F-09.03）→ `{ok,latency_ms,error_code}`，展示实测延迟，**不缓存探测结论**。
- 就绪检查：`GET /ready`（F-09.04）→ `{ready,checks,version}`，逐项列出 `checks`。
- **密钥红线**：`key_configured` 只显示"已配置 / 未配置"，**不回显、不可编辑、不写日志**（DESIGN_REVISION §7）。
- **`embedding_model` 变更**：若返回 **409 `REINDEX_REQUIRED`**，必须显式提示"变更 embedding 模型视为更换向量空间，需重建索引后方可生效"，**不静默丢弃该错误**。向量空间切换另有配置项 `dimension`；**不存在"配额耗尽热降级到本地模型"的路径**（FUNCTION-MAP §1 `config（Provider）`）。
- `top_k` 是 `answer_top_k` 的 API 别名；`vector_top_k` / `keyword_top_k` / `max_per_route` / `rrf_k` 为独立字段，**前端不得用单字段同时修改四值**（API-CONTRACTS §6）。

## 5. 横向交互规则

### 5.1 请求层（H30 `api_request`）

1. 附加 `Authorization: Bearer <access_token>`；
2. **401 共用一次 refresh**：并发 401 只触发一次 `POST /api/auth/refresh`，其余请求等待结果，**禁止无限刷新**；refresh 失败 → 清身份 → `/login`；
3. 错误映射（API-CONTRACTS §1）：401 身份无效 / 403 功能无权 / 404 防枚举 / 409 幂等或版本冲突 / 410 游标过期 / 413 超限 / 415 格式不支持 / 422 参数 / 429 限流 / 503 依赖不可用；
4. **有幂等键才自动重试写请求**；无幂等键的写请求网络失败后由用户显式重试。

### 5.2 五态与写保护

- **每个页面/面板必须实现五种状态**：加载中、空态、无权限、失败可重试、正常（PRD §1 第 5 条）。
- **写请求禁止重复提交**：按钮进入 loading 并禁用 + 携带幂等键。
- **`revision` 冲突（409）**：**不自动覆盖**，提示"内容已被他人修改"，先 `GET` 刷新最新数据，再由用户重新确认提交（PRD §1 第 5 条）。
- **错误呈现**：只展示响应 `message`（安全文案），**不展示原始异常/堆栈**。
- **两层错误码分开呈现**（API-CONTRACTS §1）：HTTP 层错误走全局提示；**任务层错误**（`index_task.error_code` 等）走任务详情内的状态展示，**HTTP 仍为成功**——不得把任务失败渲染成请求失败。

### 5.3 状态映射（前端展示用）

| 域 | 状态 | 展示 |
|---|---|---|
| 知识索引 | `pending` / `indexed` / `stale` | 待索引 / 已索引 / 已过期（需重建） |
| 索引任务 | `queued` / `running` / `retry_wait` / `succeeded` / `failed` / `superseded` | 排队中 / 执行中 / 等待重试 / 已完成 / 已失败 / 已被新版本取代 |
| 任务阶段 | `parsing` / `embedding` / `indexing` | 解析中 / 向量化中 / 建索引中（**阶段不并入状态**，PRD §1.2.2） |
| 问答请求 | `accepted` / `running` / `completed` / `rejected` / `failed` / `cancelled` | 已登记 / 生成中 / 已完成 / 已拒绝 / 已失败 / 已取消 |
| 业务结果 | `answered` / `faq_hit` / `access_restricted` / `no_evidence` / `low_confidence` / `service_error` | 回答 / FAQ 命中 / 权限受限 / 无可用知识 / 置信不足 / 服务异常（**请求状态与业务结果不是同一字段**，PRD §1.2.3） |
| FAQ | `candidate` / `published` / `rejected` / `offline` / `stale` | 候选 / 已发布 / 已驳回 / 已下线 / 来源失效 |
| 缺口 | `open` / `processing` / `closed` | 待处理 / 处理中 / 已关闭 |

- 未知状态值一律以原文展示（不吞掉），避免新增状态静默变成"正常"。

## 6. 前端契约函数落地对照

| 编号 | 契约（FUNCTION-MAP §4） | 落地位置 | 要点 |
|---|---|---|---|
| H30 | `frontend.api_request` | `src/api/client.ts` | 附令牌；401 共用一次 refresh；错误映射；有幂等键才自动重试 |
| H31 | `frontend.parse_sse` | `src/composables/useSse.ts` | 流式 UTF-8 缓存、LF/CRLF 分帧、多行 `data:` 拼接、`seq` 去重；**坏帧不静默跳过**，交给恢复流程 |
| H32 | `frontend.expand_drop` | `src/composables/useUpload.ts` | 递归枚举目录、保留展示路径、清单预校验 |
| H33 | `frontend.normalize_acl` | `src/composables/useAcl.ts` | 实体去重；保留非 `global` 选择；全空提示默认拒绝 |
| H35 | `frontend.open_citation` | `src/api/chat.ts` | 调用 `read_citation`；展示原文位置；失效显示不可用 |
| F-10.01 | `delivery.resolve_navigation` | `src/router/navigation.ts` | 过滤菜单（纯函数，可单测） |
| F-10.02 | `delivery.render_answer` | `src/composables/useMarkdown.ts` | 序号去重、rAF 批次、禁原始 HTML 与危险协议、代码高亮复制、引用定位；末批 flush |
| H27 | `chat_store.get_request` | `src/api/chat.ts` | 410 时的安全快照入口 |
| H28 | `chat_store.list_sessions` | `src/api/chat.ts` | 会话侧栏 |
| H34 | `faq_store.list_faqs` | `src/api/faq.ts` | 候选/已发布列表 |
| F-01.04 | `get_me` | `src/api/auth.ts` | 身份与权限码唯一来源 |

**纯函数可测性要求**：`resolve_navigation`、`parse_sse`、`normalize_acl`、`expand_drop`、`render_answer` 必须实现为**不依赖组件实例的纯函数/可注入函数**，以便用单元测试直接覆盖 AC-10.01/10.02 的分帧、缺序、坏帧、尾批与全空 ACL 场景。

## 7. 视觉规范

上游文档此前**未规定**颜色、字体、布局、组件库、响应式与主题（`docs/*.md` 中无相应条款）。本节为**首次定义**，仅约束表现层，不改变任何权限或安全语义。所有取值实现为 CSS 变量，Element Plus 组件经主题变量覆盖后统一取用。

### 7.1 设计原则

企业数据密集型控制台：**信息密度优先、弱装饰、层级清晰**；颜色只用于表达**语义**（状态、危险、可交互），不用于装饰。数据表与状态是主角，动画仅用于反馈（≤200ms）。

### 7.2 颜色令牌

| 令牌 | 亮色 | 暗色 | 用途 |
|---|---|---|---|
| `--kb-bg` | `#f5f7fa` | `#111418` | 页面背景 |
| `--kb-surface` | `#ffffff` | `#1a1f26` | 卡片 / 表格 / 抽屉 |
| `--kb-surface-2` | `#fafbfc` | `#20262e` | 表头 / 次级容器 |
| `--kb-border` | `#e4e7ed` | `#2b323c` | 分隔线 / 边框 |
| `--kb-text` | `#1f2329` | `#e6e8eb` | 主文本 |
| `--kb-text-2` | `#606266` | `#a3a6ad` | 次级文本 |
| `--kb-text-3` | `#909399` | `#7c848f` | 辅助 / 占位 |
| `--kb-primary` | `#1d4ed8` | `#3b82f6` | 主操作 / 选中 |
| `--kb-success` | `#059669` | `#10b981` | 成功 / `indexed` / `succeeded` |
| `--kb-warning` | `#d97706` | `#f59e0b` | 等待 / `pending` / `retry_wait` / **受限提示** |
| `--kb-danger` | `#dc2626` | `#ef4444` | 失败 / 删除 |
| `--kb-info` | `#64748b` | `#94a3b8` | 中性 / `stale` |

**语义映射规则**

- **受限提示用 `warning` 而非 `danger`**：权限受限是正常业务结果（`access_restricted`），不是系统错误。
- `superseded` / `stale` / `offline` 用 `info`，不用 `danger`——它们是生命周期状态而非故障。
- **只使用颜色不足以表达状态**：每个状态徽标必须同时带文字标签，颜色为辅助（色觉可达性）。

**图表分类色板**（8 色，明暗各一套，与主色板同族）：`#1d4ed8` `#0891b2` `#059669` `#d97706` `#dc2626` `#7c3aed` `#db2777` `#64748b`。`unknown` 用量固定使用中性灰，且**在堆叠图中单独成段**，不与已知值混色。

### 7.3 字体与排版

- 字体栈：`-apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", Roboto, "Helvetica Neue", sans-serif`。
- 等宽栈（代码/ID/路径）：`"JetBrains Mono", Consolas, "Courier New", monospace`。
- 字号阶梯：`12 / 13 / 14 / 16 / 20 / 24 / 32`。正文 `14`；表格与次级信息 `13`；页标题 `20`；指标卡数值 `32`（长数字 `24`）。
- 行高 `1.5`；数字列与 ID 列使用 `font-variant-numeric: tabular-nums` 对齐。
- 词重：正文 `400`、强调与表头 `500`、标题 `600`。**不使用 `700` 以上**，避免中文粗体发糊。

### 7.4 间距、圆角、阴影、层级

- 间距基准 `4px`：`4 / 8 / 12 / 16 / 24 / 32 / 48`。面板内边距 `16`，面板间距 `16`，页面内边距 `24`。
- 圆角：`2`（标签）/ `4`（输入、按钮）/ `6`（卡片、抽屉）/ `8`（弹窗）/ `999`（徽标）。
- 阴影三级：`--kb-shadow-1`（卡片，`0 1px 2px rgba(0,0,0,.06)`）/ `--kb-shadow-2`（下拉、浮层）/ `--kb-shadow-3`（弹窗、抽屉）。
- 表格行高 `40`，紧凑模式 `32`；表头吸顶。

### 7.5 布局与响应式

- **主导航固定左侧栏**（宽 `220`，可折叠至 `64`）；顶栏高 `56`，承载面包屑、身份信息、主题切换、退出。
- 断点与行为：
  | 宽度 | 行为 |
  |---|---|
  | ≥ 1440 | 三栏完整展开（如问答工作台：侧栏 + 对话 + 来源面板） |
  | 1024 — 1440 | 侧栏可折叠；来源面板改为抽屉 |
  | < 1024 | 左侧导航抽屉化 |
- **首版目标为桌面端**（PRD §0 第 3 条单实例控制台定位）；`< 768` 仅保证不破版（表格横向滚动、抽屉覆盖），**不声明移动端适配**。

### 7.6 暗色模式

- 通过 `html.dark` + CSS 变量整套切换，不写第二套组件样式；ECharts 在主题切换时**销毁重建**以应用暗色轴色与网格线。
- 切换项持久化到 `localStorage` 的主题键（**仅主题偏好，不含任何身份或令牌**）。
- 首屏在 `index.html` 内联脚本读取主题键，避免暗色闪白。

### 7.7 Markdown 渲染样式（F-10.02）

- 代码块：等宽字体、暗色底、右上角复制按钮、横向滚动；**代码内容按文本渲染**，不当作 HTML。
- 表格：横向滚动容器，表头吸顶。
- 引用角标：上标序号，主色，可点击定位到右侧引用卡。
- 链接：外链图标 + `rel="noopener noreferrer"`；**协议不在白名单内时不渲染为可点击链接**。
- 长答案不阻塞滚动：`delta` 批次渲染期间保持滚动锚点（用户手动上滚后不再自动跟随）。

### 7.8 可访问性与文案

- 文本对比度 ≥ `4.5:1`；焦点可见；所有图标按钮有 `aria-label`；弹窗焦点陷阱，`Esc` 关闭。
- 表单错误与后端 `message` 通过 `aria-live` 播报。
- **固定文案不得改写**（FUNCTION-MAP §1）：
  - `PARTIAL_RESTRICTED_NOTICE` = 「部分参考资料因权限受限无法展示」（部分无权，原文不可改）；
  - `ACCESS_RESTRICTED_NOTICE` = 「该问题超出当前权限范围，无法作答」（全部无权），**不得含数量**。
- 展示受限提示时**不得附带受限条数、被拒知识数量或任何 ID**。

## 8. 前端安全红线（不得放宽）

1. **access token 与 refresh token 只存内存**，不落 `localStorage` / `sessionStorage` / URL / 日志（API-CONTRACTS §2；页面刷新后重新登录是首版行为）。
2. **SSE 用 `fetch` 携带 `Authorization`**，不把 token 写查询参数。
3. **菜单与按钮显隐不是安全边界**：所有受控操作后端必须复核（AC-10.01-01/02、U01）。
4. **受限提示只含固定 message**，不含受限 ID / 标题 / 摘要 / 部门名单 / 数量（`reason_code=ACCESS_RESTRICTED`）。
5. **Markdown 禁止不受控 HTML**；链接协议白名单；代码块按文本渲染。
6. **不把传输完成当索引完成**；**不把本地缓存当授权事实**（FUNCTION-MAP §2.4"未授权条目不从本地旧缓存补回"）。
7. **图表不用上次缓存冒充当前范围失败结果**。
8. **不序列化 ORM 行/原始异常到界面**；错误只展示安全 `message`。
9. **前端不生成任何身份或权限字段**（原参考工程的 `X-Role`/`X-Tenant-Id` 自报头做法**明确禁止**，REUSE-MATRIX R11）。

## 9. 界面验收对照（PRD §1.4「界面验收」）

| 验收项（PRD §1.4） | 对应页面/能力 | 落点 |
|---|---|---|
| 文件夹部分失败 | 知识中心·导入抽屉 | §4.3 第 5 步：逐项 `error_code` + 单独重试 |
| 刷新进度 | 知识中心·导入抽屉 | §4.3 第 4 步：F-03.03 轮询恢复真实进度与错误 |
| 无权按钮直接接口调用 | 全局 | §3：后端仍拒绝；401/403 分路径处理 |
| SSE 分帧 / 重连 / 停止 | 问答工作台 | §4.4：H31 分帧、`after_seq` 重订阅、410 安全快照、F-05.07 |
| 引用撤权 | 问答工作台·引用卡 | §4.4 H35；失效显示不可用 |
| FAQ 审核与关闭缺口 | 沉淀运营页 | §4.5 状态机与回放规则 |
| 直接 URL 访问受控 | 全局路由 | §3 前置守卫 + 后端授权 |
| 流式 Markdown 安全渲染 | 问答工作台 | §7.7、§8 第 5 条 |
| 菜单随身份刷新同步 | 全局导航 | §3 身份刷新规则 |

**可测性要求**：H31/F-10.02 的分帧、跨 UTF-8 多字节分块、缺序、重复 `seq`、坏帧、末批 flush 必须有单元测试；H33 全空 ACL 必须有单元测试；F-10.01 菜单过滤按权限码组合必须有单元测试。

**已落地（2026-09-17）**：上述要求由 `frontend/tests/` 下 4 个用例文件共 **73 项**兑现（`npm test`，Vitest + jsdom）。
其中"末批 flush""跨 UTF-8 多字节分块""坏帧不静默""重复 `seq` 幂等""缺序标记 gap"逐条对应；安全类断言用真实 DOM 检查（`querySelector`）而不是字符串匹配——转义后的文本里同样会出现 `onerror=` 这样的字面量，字符串断言会给出假阳性。

## 10. 实现状态与未决项

### 10.1 实现状态（2026-09-17 更新）

本文 §2—§9 已落地为**独立前端工程 `frontend/`**（Vue 3 + Vite + TypeScript + Element Plus + Pinia + ECharts + markdown-it/DOMPurify/highlight.js），共 **40 个源文件 / 6183 行**；§9 可测性要求已落地为 **4 个测试文件 / 73 项单元测试**。

**已验证（命令与输出）**

| 步骤 | 命令 | 结果 |
|---|---|---|
| 依赖安装 | `npm install` / `npm install -D vitest jsdom` | exit 0 |
| 类型检查 | `npx vue-tsc --noEmit` | exit 0，无 TS 报错 |
| 单元测试 | `npm test`（`vitest run`） | **exit 0，4 files / 73 passed**（`stream` 17、`markdown` 27、`acl` 15、`navigation` 14） |
| 生产构建 | `npx vite build` | exit 0，15.46 s，2505 modules transformed |

**测试落地位置与覆盖面**

| 契约 | 测试文件 | 覆盖要点 |
|---|---|---|
| H31 | `frontend/tests/stream.spec.ts` | 分帧（LF/CRLF）、帧边界跨 chunk、**跨 UTF-8 多字节分块**（含每字节分块）、多行 data、heartbeat 不推进游标、重复 `seq` 幂等、**缺序标记 `gap`**、坏帧/缺 seq/未知事件标 `malformed`、末批残帧 `TRUNCATED_FRAME` |
| F-10.02 | `frontend/tests/markdown.spec.ts` | 事件重排与去重、`last_seq` 基线语义、终态与用量、部分/全部无权**固定文案逐字**、危险协议与原始 HTML 不产生元素、代码块按文本渲染、表格容器、**rAF 末批 flush**、H35 引用提示 |
| F-10.01 | `frontend/tests/navigation.spec.ts` | 权限码组合（任一/全部/未知码不放行）、默认落点、分组顺序、纯函数不改入参 |
| H33 | `frontend/tests/acl.spec.ts` | 去重保序、非有限值剔除、**全空即拒绝所有人**、提示文案逐字且不含数量、变更比对 |

**本轮由测试发现并修复的实现缺陷**：`useMarkdown.ts` 的 DOMPurify 配置用了 `ALLOWED_URI_REGEXP` 做链接协议白名单。DOMPurify 会对**每个**非惰性属性的值做"必须匹配该正则"的校验，于是 `target="_blank"` / `rel="noopener noreferrer"` 这类与 URI 无关的合法值被判为不安全而**整条删除**，直接违反 §7.7。已改为 `uponSanitizeAttribute` 钩子**只约束 `href`**，并同时移除 `USE_PROFILES`（它会重置允许属性集合，与 `ADD_ATTR` 叠加时行为难以预期）。

**未验证（不得据此声称端到端可用）**

- 后端目前只实现了 M01（`POST /api/auth/login|refresh|logout`、`GET /api/auth/me`）与 `GET /health`（FUNCTION-MAP §2.2「M01 实现状态」）。**M02—M09 的路由与服务尚未实现**，因此本前端目前**只有登录链路可以与真实后端联通**；其余页面在真实数据下未验证。
- Milvus / Neo4j 的业务链路未接入（中间件本身已在 VM 上验证可用）。
- 未跑浏览器端 E2E；H32 上传（目录递归枚举、410 安全快照恢复、`SESSION_BUSY` 冲突路径）、H30 单飞 refresh 与 401 处理**仍无单元测试**，且未在真实环境验证。
- `parseSse` 只覆盖"给定字节流 → 事件序列"，**没有覆盖真实网络的中断与重连**。

**落地位置对照（§6 的兑现情况）**

| 编号 | 落地文件 |
|---|---|
| H30 | `frontend/src/api/client.ts` |
| H31 | `frontend/src/composables/useSse.ts` |
| H32 | `frontend/src/composables/useUpload.ts` |
| H33 | `frontend/src/composables/useAcl.ts` |
| H35 | `frontend/src/api/chat.ts` + `frontend/src/components/CitationCard.vue` |
| F-10.01 | `frontend/src/router/navigation.ts` + `frontend/src/router/index.ts` |
| F-10.02 | `frontend/src/composables/useMarkdown.ts` + `frontend/src/components/MarkdownView.vue` |

### 10.2 未决项

| 项 | 现状 | 影响 |
|---|---|---|
| **视觉规范** | 本文 §7 首次定义，上游无规定；已按此实现 | 如需调整需改 `frontend/src/styles/tokens.css`（单一来源） |
| 前端工程落点 | **已按本文建议新建 `frontend/` 独立工程**；参考工程 `General-PurposeRAG/frontend/` 未改动（仍为只读复用候选） | 后续若要沿原工程改造需重新评估（REUSE-MATRIX R09/R10） |
| 生产托管方式 | DEPLOYMENT 规划由反向代理（`kb-web`）托管静态产物；`backend/app/` 目前**无** `StaticFiles`/CORS 配置 | 生产部署前需二选一；开发态已用 Vite proxy 规避 |
| 设计工具链 | **pen.dev 扩展已安装并登录**（`highagency.pencidev` 0.6.71）。MCP 服务端注册名是 **`pencil`**；已在 `~/.codebuddy/mcp.json` 补注册，**本会话可正常调用** `read_skill` / `get_app_state` / `execute` / `get_style`。**硬前置：编辑器中必须有 `.pen` 文件处于打开状态**，否则所有工具返回 `Failed to access file`。CLI 侧 `@pen.dev/cli` 0.3.7 仍未认证（`pen login` 仅交互式） | `execute` 的 `filePath` 实际跟随**当前打开的文档**，不会切到仓库里的文件；故设计画在已打开的画布上 |
| 画布设计稿 | **已在 pen.dev 画布完成 7 屏设计**（每屏 1440×900，Light+Dark 双主题，取值全部走 `$变量`）：M05 问答工作台、M03/M04 知识中心、M08 运营看板、M06/M07 沉淀运营、M02 组织配置、M09 模型配置、M01 登录页；顶栏抽为可复用组件（`reusable: true` + `ref` 覆盖标题）。结构体检（`ctx.bounds` + `ctx.problems`）**7 屏全部无塌陷、无溢出**；PNG 已导出到 `frontend/design/exports/`（7 张，236—356 KiB）。已保存并同步为 **`frontend/design/kb-platform.pen`**（229,241 B；schema `version: 2.17`；**8 个顶层节点 = 7 屏 + 1 个 TopBar 可复用组件**；18 个变量 / Light+Dark；514 节点 / id 全唯一） | 无未决。`build-pen.mjs` 生成器与旧草稿**已删除**，画布是设计的唯一来源 |
| 前端测试工具 | 单元测试已接入 **Vitest 2.1.8 + jsdom 25**（`npm test` / `npm run test:watch`；配置在 `vite.config.ts` 的 `test` 段，与构建共用同一份 `@` 别名，避免两套解析规则）；B 端 **E2E 工具仍未选型** | F-10.03 端到端验收的自动化程度仍取决于 E2E 选型 |
| 角色默认权限映射 | DESIGN_REVISION §2.1 仅有"系统管理员默认管理组织、功能授权、配置和看板" | 已按"新建角色默认不勾选"实现，需确认 |
| 后端地址 | 开发态由 `.env.development` 的 `VITE_API_TARGET` 决定（默认 `http://127.0.0.1:8000`） | 后端实际监听端口确定后需同步 |
