# 知识库管理平台

目标：落地可部署、可演示、可复现评测的通用知识管理与 AI 问答平台，作为简历与面试的真实工程项目。保留多格式知识维护、四维权限、授权问答、运营看板及 FAQ/缺口闭环，依据原需求 2.9.1—2.9.10。客服仅是演示场景，不转为电商或售后专用产品。

当前状态：权限基座、持久层、DTO 层与认证端点已落地，**PA-01—PA-15 审计项全部关闭**。**M01 身份认证**与 **M02 组织用户与功能权限**（F-02.01—F-02.09 + API-S01/S02/S03）已实现；**M03 文档导入解析与任务已完整落地**：受理与任务生命周期（F-03.01/02/03/05/06 + 租约原语 H14/H15，`POST /api/uploads` 等 4 条路由）与**索引流水线 F-03.04**（H19/H20/H21 解析清洗切片 → H11 向量化 → H16/H17 写入激活，`run_pipeline` 全程租约防护）。**真实链路已补验**：DashScope 向量化 + Milvus 写入/计数核对/最近邻检索（命中自身 distance=1.0000），流水线终态收敛（成功/superseded/瞬时 retry_wait/永久 failed）由 5 项集成测试钉住。**M04 知识生命周期与四维权限已落地**（F-04.01—F-04.09 + API-S04：台账/元数据/切片阅读/替换文档/切片编辑/启停/删除墓碑/四维 ACL/实体选择；数据读权走 H04 `authorize_units`，无权与不存在同样 404 防枚举；切片编辑重存源文件，重索引不丢人工编辑）。**M05 会话检索与流式问答已落地**（F-05.01—F-05.09 + F-08.01：会话/幂等受理/后台执行/两路召回 RRF 融合/DashScope 流式生成/SSE 断线续订/协作取消/引用定位/联想；全无权固定拒答、终态唯一事务、撤权即时生效——执行侧从 DB 重装配身份）。Alembic 已至 `0002`——该迁移补齐 `DATA-CONTRACTS §2` 声明的 `upload_batch` / `upload_item`，38 张业务表已在真实 MySQL 8.0.26 执行（两库各 39 表，含 `alembic_version`），种子数据与集成测试通过，**M07 知识缺口闭环已落地**（F-07.01—F-07.04 + API-S06：outbox 登记与消费、部门指纹聚合、转建/绑定/回放关闭——回放用原提问用户当前身份）。**M06 FAQ 沉淀审核与缓存已落地**（F-06.01—F-06.07：挖掘管线 H22—H25、代表中心聚类、起草不占长事务、无独立 ACL、来源失效转 stale 并接入知识替换/启停/删除路径）。**M08 看板已落地**（F-08.02/03：PV/UV、命中率/覆盖率/错误率、六类图表——UTC 存储、服务端上海日/周分桶）。全量 `pytest` **359 passed / 0 skip**，文档-代码一致性门禁 exit 0（无新增漂移）。**前端骨架已落地**（[前端规格](docs/FRONTEND-SPEC.md) §2—§9 对应 `frontend/` 独立工程：Vue 3 + Vite + TS + Element Plus + Pinia + ECharts，40 个源文件 / 6183 行；`npx vue-tsc --noEmit` 与 `npx vite build` 均 **exit 0**；`npm test` **73 passed**，覆盖 H31 分帧 / F-10.01 菜单过滤 / F-10.02 渲染与安全 / H33 全空 ACL；开发态经 Vite proxy 对接后端）。**仍未宣称平台可运行**：M05 之后的业务路由（会话/问答/FAQ/挖掘/看板）与 `services` 尚未落地，前端当前只有登录链路可与真实后端联通；中间件侧（VM 上 Milvus v2.5.5 + Neo4j 5.21.0，D-08）端到端往返已验证、后端配置已切到 `milvus_standalone` 形态并实测连通，但**检索链路对中间件的使用尚未实现**（M05 范围）。逐项差异与关闭证据见 [权限契约审计](docs/PERMISSION-CONTRACT-AUDIT.md)；中间件与配置见 [部署](docs/DEPLOYMENT.md)。

版本：2026-09-16 R3；当前只整理文档，确认前不开发。

## 当前文档

- [交付、评测与文档确认](docs/DELIVERY-EVALUATION.md)：通用定位、阶段出口、评测方法、简历证据与未决事项。

- [PRD](docs/PRD.md)：10模块、60功能点、180验收项及边界。
- [FUNCTION-MAP](docs/FUNCTION-MAP.md)：60功能函数、36基础函数、49前端操作契约及调用关系。
- [ARCHITECTURE](docs/ARCHITECTURE.md)：当前架构、授权、索引、问答与沉淀状态。
- [PLAN](docs/PLAN.md)：实施阶段、验收出口、已决与待决事项。
- [DEPLOYMENT](docs/DEPLOYMENT.md)：单实例受控发布、持久化与恢复设计。
- [DESIGN_REVISION](docs/DESIGN_REVISION.md)：已确认修订来源；其中历史“尚无实现”是当时状态，当前以本页为准。
- [WORKLOG](docs/WORKLOG.md)：历史操作和验证记录，不代替当前验收。
- [REUSE_NOTES](docs/REUSE_NOTES.md)、[INTERVIEW](docs/INTERVIEW.md)：历史参考，技术事实和面试表述需按当前实现核对，不能覆盖正式规格。

阅读顺序：原始需求→PRD→FUNCTION-MAP→ARCHITECTURE→PLAN/DEPLOYMENT。

## 核心边界

- 四维OR、直属部门、默认拒绝；无创建者/超管正文旁路。
- 授权之后有权重排；部分受限保留有权回答；全部受限固定提示。
- MySQL最终状态、版本索引、任务租约、持久请求与事件；FAQ多来源逐一授权。
- 原文/文件持久保存；缓存和向量可重建但不作为授权事实源。
- 首版单实例维护窗口发布。首轮DOCX/文本PDF/MD/TXT，DOC/OCR后续扩展；演示使用云端API与模拟资料。具体模型、保留期、容量及恢复目标仍需确认。

## 架构图与参考项目

[R3 架构流程图册](docs/diagrams/README.md)含七张设计图及JSON图源；图形结构与桌面显示已检查。图册描述目标设计，不代表代码已实现。

General-PurposeRAG是可运行基线与组件复用候选；原目录当前保持不变。先验证完整链路和改造耦合，再选择沿原工程改造或新骨架迁入。原有能力不计作本人新增完成量，历史结果不代替本平台验收。详见 [复用与改造矩阵](docs/REUSE-MATRIX.md)。

## 演示目标

客服FAQ审核发布与知识缺口补档完整链路，加财务薪酬四维权限正反例。所有完成结论须保留版本、步骤和实际结果。

## 文档确认补充

- [前端规格](docs/FRONTEND-SPEC.md)：七个页面的路由表、页面结构、交互逻辑、前端契约函数落地与视觉规范；§10 记录实现状态、验证命令与未验证项。
- [API 与协议](docs/API-CONTRACTS.md)：字段、分页、鉴权、幂等、SSE及完整入口登记。
- [数据实体与事务](docs/DATA-CONTRACTS.md)：逻辑字段、唯一键、关联和故障一致性。
- [格式与验收边界](docs/FORMAT-ACCEPTANCE.md)：支持矩阵、建议评测门槛与待确认参数。
- [契约决策记录](docs/adr/ADR-0003-contract-closure.md)：修改动机、兼容和验证边界。
- [权限契约审计](docs/PERMISSION-CONTRACT-AUDIT.md)：现有权限代码与 R3 安全契约的 15 项差异、修正顺序与 D1 门禁。

当前仍为文档确认阶段，尚不进入业务开发。

本轮结果与未验证项见 [文档审查记录](docs/DOCUMENT-REVIEW.md)；权限代码差异见 [权限契约审计](docs/PERMISSION-CONTRACT-AUDIT.md)。
