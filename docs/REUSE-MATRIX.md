# 原项目复用与改造矩阵

日期：2026-09-16。状态：源码静态核对后的路线提案；**B1 最小环境与启动基线已于同日复验通过**（见 [基线验证](BASELINE-VALIDATION.md)），尚未实施改造。

## 1. 目标与建议

保持通用知识管理与问答平台、原60功能及R3安全契约。优先验证 General-PurposeRAG 能否成为可运行基线，再决定“沿原项目渐进改造”或“新骨架迁入组件”；不再提前锁死仅允许少量代码移植，也不承诺整套原样沿用。

当前 General-PurposeRAG 原目录保留为未修改的来源快照。若选择原地架构演进，应在隔离分支/工作副本中开发，“原项目基础改造”不等于破坏唯一参考副本。当前 backend 只有部分基础和权限代码，应纳入比对，不同时维护两套业务事实源。

B1 验证在隔离副本 `baseline-b1/` 中进行（robocopy 复制，排除 `.venv` 等；`uv.lock` SHA256 与快照一致且 `--frozen` 后未变），**来源快照至今零修改**。副本内含两处 B1 偏差：新增 `README.md` 占位文件（快照缺失而 `pyproject.toml` 引用）、`.env` 改名为 `.env.b1-disabled`（快照内含真实凭据）。两处及快照缺陷详见 [基线验证](BASELINE-VALIDATION.md)。

## 2. 实现证据与复用矩阵

“有源码”仅证明实现存在，不证明功能正确、兼容已选模型或可部署。相对路径以 General-PurposeRAG 为根。

| 编号 / 对应目标 | 源码与当前证据 | 复用方式 | 必须改造的部分 | 验证出口 |
|---|---|---|---|---|
| R01 模型向量化 H11 | [embeddings/client.py](../General-PurposeRAG/src/embeddings/client.py)存在客户端 | 候选适配层；待逐函数审查 | 统一模型版本/维度/超时/用量；禁止跨模型空间热降级 | 固定模拟文档，向量数量/顺序/维度一致，失败显式报告 |
| R02 重排 H12 | [rerank/client.py](../General-PurposeRAG/src/rerank/client.py)处理两种请求结构及空文本索引映射 | 改造复用，非直接复制 | 失败退Mock应改为显式“授权融合排序降级”；不同分数不能无说明混排 | 空文、乱序下标、超时、无效分数、降级路径；外发仅授权正文 |
| R03 向量召回 H08 | [retrievers/dense.py](../General-PurposeRAG/src/retrievers/dense.py)有集合与搜索适配 | 抽离Milvus适配逻辑 | 原实现绑定dense+sparse、租户集合；改版本元数据与状态复核，异常不能等同无命中 | 真实Milvus读写/版本/删除/故障，服务失败不入缺口 |
| R04 融合 H09 | [graph/nodes/rrf_fusion.py](../General-PurposeRAG/src/graph/nodes/rrf_fusion.py)存在融合节点 | 算法候选，解耦图状态后复用 | 统一ScoredChunk、去重键、稳定排序，授权后融合 | 手算排名、同分、重复、空路；RRF不作为置信度 |
| R05 解析 H19 | [parsers/plain_text.py](../General-PurposeRAG/src/parsers/plain_text.py)直接UTF-8读取、errors=ignore、字符滑窗 | 仅TXT/MD原型参考 | 禁止忽略乱码；不拿此解析器读DOCX/PDF；页码和偏移契约补齐 | 格式矩阵中正常/损坏/乱码/空文；扫描明确不支持 |
| R06 切片 H20/H21 | [indexers/splitters.py](../General-PurposeRAG/src/indexers/splitters.py)存在两级/语义相关逻辑与测试文件 | 按需求选函数，待算法专项评测 | 统一tokenizer、500/50初值、版本化稳定seq和来源映射 | 标题、条款、金额、否定词及边界不被清洗破坏 |
| R07 导入 M03 | [pipeline/batch_ingest.py](../General-PurposeRAG/src/pipeline/batch_ingest.py)与[documents路由](../General-PurposeRAG/src/api/routers/documents.py)存在 | 复用文件处理细节，任务层重做 | DB任务/租约/幂等上传/条件激活/墓碑清理 | 失败恢复、批次逐文件映射、旧任务不覆盖新版本 |
| R08 问答 M05 | [routers/chat.py](../General-PurposeRAG/src/api/routers/chat.py)POST直接生成SSE并输出召回信息 | 保留生成适配候选，重写编排出口 | 新建请求与订阅分离，鉴权先于外部重排和Prompt；禁止未审查召回内容直出 | 公开/受限混合、全拒绝、幂等、断线、来源撤权 |
| R09 SSE前端 H31 | [useChatStream.ts](../General-PurposeRAG/frontend/src/composables/useChatStream.ts)支持LF/CRLF分帧及rAF | 改造复用UI更新方式 | 现有解析无seq，坏JSON跳过，多行data直接拼接；补协议解析、恢复、应用后游标 | UTF-8跨帧、多行data、缺序/重复、坏帧、尾批、断线 |
| R10 上传/页面 M03/M10 | [useUpload.ts](../General-PurposeRAG/frontend/src/composables/useUpload.ts)、Vue/Vite工程 | 页面结构与交互候选 | 对齐路由/DTO/按钮权限；传输与索引进度分离，避免自报角色 | 前端构建、直接API越权、部分上传失败和刷新恢复 |
| R11 身份权限 M01/M02/M04 | [api/deps.py](../General-PurposeRAG/src/api/deps.py)读取X-Role，未配置API Key时放行 | 必须替换安全事实源 | JWT/持久会话、组织角色、四维OR、默认拒绝、实时身份 | 默认/创建者/超管拒绝，四维真值、注销与调岗生效 |
| R12 会话审计 M05/M08 | [api/session.py](../General-PurposeRAG/src/api/session.py)文件/内存会话；audit目录存在 | 复用数据表达思路，持久层重做 | MySQL归属、请求事件、统一终态事务、真实用量、outbox | 并发/重启/撤权/终态重复，日志可重算 |
| R13 FAQ/缺口 M06/M07 | 本次未找到足以证明R3闭环已实现的代码证据 | 按新契约新增，不能计作原项目完成量 | 聚类消费、审核、多来源授权、缓存、补档绑定与原身份回放 | 从终态日志到FAQ和缺口关闭的完整演示 |
| R14 测试/评测 M10 | tests与scripts有切片、检索、问答、会话等入口 | 复用测试结构及有用夹具 | 去除旧权限预期，建立业务相关文档与身份标注 | 旧基线和新契约分开报告；不把Mock通过当真实模型通过 |
| R15 当前backend | [permission.py](../backend/app/engines/permission.py)及core/tests已有 | 逐项比较后留用 | 取消超管/创建者旁路和祖先部门匹配；配置不冒充安全生产默认值 | 与R3同一套权限测试通过，不建第二套鉴权引擎 |

## 3. 明确不能直接继承的行为

1. api/deps.py 的 require_api_key 在未配置时放行；require_role 信任 X-Role。客户端头不能成为企业身份事实源。
2. rerank/client.py 发生请求异常后返回 MockReranker；故障与真实重排效果必须可区分。模拟器可以用于单测，不能静默进入演示效果数据。
3. retrievers/dense.py 捕获搜索异常后跳过该路，可能返回空列表；新契约必须区分正常未命中、部分路降级和检索不可用，不能制造假知识缺口。
4. useChatStream.ts 在JSON解析失败后continue且未处理SSE id；不能原样满足持久事件重放。
5. parsers/plain_text.py 使用errors=ignore，按字符而非token切块；不可声称完整满足新格式和切片契约。
6. documents.py 的切片入口当前为POST且使用default_chunks；旧复用说明写GET已过时，必须从已检查源码生成接口差异表。
7. dashboard.py 当前有Depends(require_api_key)，旧“完全无鉴权”描述不准确；但该检查仍不满足当前登录、功能和数据权限要求。

## 4. 基线验证计划（未执行）

| 门禁 | 操作范围 | 必须留下的证据 | 失败处理 |
|---|---|---|---|
| B0 来源快照 | 记录源码版本/关键文件哈希、许可、依赖清单；不读取或复制真实.env凭据 | 来源标识、改造工作副本说明 | 无法确定版本则先记录文件快照，不伪造上游commit |
| B1 最小环境 | 检查pyproject/uv.lock/frontend脚本与入口；隔离环境，使用新模拟配置 | 依赖安装、构建、启动记录 | 记录阻断与依赖；不默认安装全部重解析/图谱链 |
| B2 原链路 | 一个TXT/MD模拟文档：导入→索引→云模型问答→引用 | 实际请求/响应、模型名、耗时、错误和是否使用Mock | 先归因配置/缺文件/实现缺陷，不能直接宣称原项目不可用 |
| B3 可切割性 | 判断Provider、检索、解析与前端是否能脱离旧全局状态 | 调用图、导入依赖、接口差异 | 确定是保留编排还是抽出组件，禁止同时维护双链路 |
| B4 新契约探针 | 在后续获准开发时验证新身份、授权过滤、请求生命周期 | 正反例与改造量清单 | 未满足安全契约前仅本地模拟演示，不面向真实用户 |

B0可继续在文档阶段做只读盘点；B1—B4涉及运行环境、云调用或代码试验，本轮未执行，待用户确认开发/验证范围后开展。云端API选择已确认，但不是已获得有效密钥或已指定供应商。

## 5. 路线选择与兼容策略

路线A：保留原Vue/FastAPI工程和能运行的RAG组件，在隔离副本中先替换身份与授权，再替换MySQL持久化、任务与事件协议，最后增加FAQ/缺口。条件是B1/B2跑通且B3证明旧耦合可控。原LangGraph可以作为过渡编排；若最终保留则另行确认ADR，不能偷偷改变当前普通async服务目标。

路线B：保留当前平台骨架，迁入R01—R10中通过验证的组件；适合旧工程启动/依赖/耦合成本明显高于迁入时。并非“全部重写”，也不限定只能复用几段代码。

当前推荐顺序为先验证A，再与B比较，不给复用百分比或节省人日。比较维度：保持可运行链路的能力、安全改造覆盖面、持久层耦合、依赖负担、可测试性、本人可讲清的贡献边界。基线失败不是自动否决，需评估修复是否有界。

迁移时旧chat接口与新请求/事件协议不得让前端混用；明确版本或一次切换。文档ID/切片ID建立映射或重建模拟索引，不复用不相容向量。仅允许保留安全的适配层，不能通过兼容开关恢复X-Role或默认放行。用户当前只授权文档，本轮不创建运行副本、不安装依赖、不调用云模型。

## 6. 来源与简历

本地LICENSE标注MIT，详细许可证文件随实际复用代码保留；依赖许可和素材来源另行盘点。本文件不对其他平台许可证作未经核验的法律判断。简历区分“原有组件”“本人重构”“新增功能”“实测结果”；可表述为在通用RAG项目基础上完成企业知识权限与运营能力改造，但仅在实际完成后使用。

## 7. 当前结论

可复用价值有明确源码证据；安全、持久化和协议必须改造；整项目改造是否更省成本尚待B1—B3验证。正式目标仍是R3，七张图描述目标架构，不应被误读为每个框都必须从零重写。
