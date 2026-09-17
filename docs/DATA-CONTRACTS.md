# 数据实体、约束与事务边界

版本：2026-09-16 R3。逻辑数据设计；不代表已执行建表或迁移。命名供实现使用，字段类型、约束和事务须在真实 MySQL 集成中验证。

## 1. 公共规则

主键 BIGINT 且对外不超过 JS 安全整数；除关联表外具有 created_at/updated_at DATETIME(6) UTC；可编辑实体 revision BIGINT 初值1。文本 utf8mb4，用户名和幂等键采用明确规范化/二进制比较，禁止依赖默认排序规则决定安全身份。状态字段 VARCHAR(32)，数据库/服务枚举校验；JSON 仅存结构化快照或扩展元数据，不代替外键。

以下“?”表示可空；其余必须提供。时间范围为半开区间。计数非负，版本>=1，indexed_version 可空且不得大于 content_version。公开DTO从白名单组装，数据库行不能直接序列化给前端。

## 2. 实体字典

| 实体 | 主要字段（除公共字段） | 关键约束与访问路径 |
|---|---|---|
| department | id,parent_id?,name,revision | parent FK自身；禁止环；存在子部门、用户或ACL引用时拒绝删除 |
| user | id,username,username_norm,password_hash,dept_id?,enabled,identity_revision,revision | UNIQUE username_norm；dept FK；停用不删历史归属 |
| role | id,name,name_norm,revision | UNIQUE name_norm；被用户或ACL引用时拒绝删除 |
| role_permission | role_id,code | PK(role_id,code)，code取14项白名单 |
| user_role | user_id,role_id | 联合PK，双FK |
| auth_session | id UUID,user_id,expires_at,revoked_at? | 登录会话稳定标识，与聊天会话分离 |
| refresh_token | id UUID,auth_session_id,token_hash,expires_at,consumed_at?,revoked_at?,replaced_by? | UNIQUE token_hash；原子消费；不存原文 |
| knowledge_unit | id,code,title,format,category,creator_id,enabled,is_deleted,global,content_version,indexed_version?,index_status,acl_version,revision | code唯一；默认global=false、无ACL；creator仅审计不赋读权 |
| knowledge_acl_department / role / user | unit_id,subject_id | 三张强类型关联表，联合PK及目标FK；接口仍使用四维数组 |
| knowledge_version | id,unit_id,version,file_key,sha256,parser_version,chunk_config,embedding_model_version,index_generation | UNIQUE(unit_id,version)；file_key是受控存储键，不是用户路径 |
| chunk | id,unit_id,version,seq,text,token_count,location | UNIQUE(unit_id,version,seq)；复合FK对应版本；稳定向量键由版本+seq推导 |
| upload_batch | id,user_id,client_batch_id,payload_hash,status | UNIQUE(user_id,client_batch_id) |
| upload_item | id,batch_id?,user_id,client_file_id,payload_hash,relative_path,unit_id?,task_id?,error_code? | UNIQUE(user_id,client_file_id)；单文件也登记，失败文件能对应原清单 |
| index_task | id,unit_id,target_version,status,stage,attempts,next_retry_at?,lease_until?,lease_token?,worker_id?,error_code?,revision | UNIQUE(unit_id,target_version)；索引(status,next_retry_at)；同一失败任务条件重试 |
| cleanup_task | id,unit_id,deletion_id,status,lease_until?,lease_token?,attempts,error_code? | UNIQUE(deletion_id)；删除墓碑与任务同事务 |
| chat_session | id,user_id,title,is_deleted,revision | 索引(user_id,updated_at,id)；不是auth_session |
| chat_request | id,user_id,auth_session_id,session_id,client_request_id,payload_hash,question,status,result_type?,config_revision,execution_lease_until?,last_seq,cancel_requested,accepted_at,finished_at? | UNIQUE(user_id,client_request_id)；索引(user_id,accepted_at,id)；auth_session_id用于注销取消，session_id指chat_session；会话行锁防同会话并发問答 |
| chat_message | id,session_id,request_id,role,text?,restricted | UNIQUE(request_id,role)，首版每轮user与assistant各一条 |
| message_source | message_id,unit_id,version,chunk_id,no | PK(message_id,no)；引用对应版本；历史读取重新授权 |
| chat_event | request_id,seq,event,payload,created_at | PK(request_id,seq)；seq由请求行锁或原子计数分配 |
| qa_audit | request_id,user_id,asked_at,question,recall_snapshot,allowed_snapshot,denied_snapshot,status,duration_ms?,first_token_ms?,usage_status | request_id唯一且FK；接受时建记录，终态更新，不只记录成功 |
| model_call_usage | id,request_id?,mining_run_id?,call_id,kind,model_version,input_tokens?,output_tokens?,units?,status,latency_ms? | UNIQUE call_id；kind=embedding/rerank/generation；后台挖掘成本不丢弃 |
| faq | id,question,answer,status,revision,cache_enabled,frequency,confidence?,cluster_id?,published_at? | 不建独立扩权ACL；published仍逐次检查来源 |
| faq_source | faq_id,unit_id,version | PK(faq_id,unit_id)；同一FAQ同一单元只引用一个版本，禁止混版本发布 |
| mining_run | id,trigger,pipeline_version,status,lease_token?,lease_until?,consumed,candidates,failed,error_code? | 运行登记与领取分离；手动动作另由幂等表约束 |
| mining_consumption | log_id,pipeline_version,run_id,status,lease_token?,lease_until? | PK(log_id,pipeline_version)；晚提交记录可被后续扫描领取 |
| question_cluster / cluster_member | cluster:id,representative,version；member:cluster_id,log_id | member联合PK；频次由去重成员数计算 |
| faq_draft_job | job_key,run_id,cluster_id,payload_hash,answer?,sources_snapshot,status,model_version | job_key唯一；模型成功结果复用，远程调用不放长事务 |
| knowledge_gap | id,department_key,fingerprint,question,status,first_seen_at,last_seen_at,max_similarity?,score_type,model_version,suggested_category?,revision | UNIQUE(department_key,fingerprint)；department_key=0表示无部门，真实部门ID>0 |
| gap_request | gap_id,request_id | 联合PK；历史部门快照固定，不随调岗重写统计 |
| supplement_task | id,gap_id,status,unit_id?,target_version?,revision | 每缺口首版一个补充任务，UNIQUE gap_id；绑定版本后回放 |
| config_revision | id,patch,snapshot,actor_id,created_at | 只保存允许的非密钥参数和secret_ref；问答绑定修订号 |
| operation_log | id,actor_id?,action,resource_type,resource_id?,trace_id,before?,after?,status,created_at | 敏感字段脱敏；读取审计也留痕，禁止记录凭据 |
| outbox_event | id,event_key,kind,payload,status,attempts,next_retry_at?,lease_token?,lease_until? | UNIQUE event_key；确保终态后缺口/缓存通知不丢失 |
| idempotency_record | actor_id,operation,client_key,payload_hash,resource_id,created_at | 联合PK；用于补档转建、手动挖掘等已有client_action_id操作 |

关联关系：user→auth_session→refresh_token；unit→version→chunk；unit→ACL及index_task；chat_session→request→message/event/audit；message/FAQ→source→unit/version；audit→consumption→cluster→FAQ；audit→gap_request→gap→supplement_task→unit/version。

## 3. 事务不变量

1. 上传：原文件先写临时受控目录并校验完整性，移动到稳定版本键后，在一事务写unit/version/task/item。DB失败文件暂成孤儿，由有保留窗口的清理扫描回收；DB不得引用尚未完整写入文件。文件移动不宣称与DB原子。
2. 索引：租约领取使用条件更新；外部解析/模型/向量写在事务外。激活以当前版本、未删除、lease_token一致为前提条件更新，并核验期望切片数；旧任务不能覆盖新版本。
3. ACL：锁定unit，验证引用实体仍可用，更新四维配置、acl_version/revision与操作日志；同事务记录失效通知。读侧以DB事实复核，不等待缓存通知才拒绝。
4. 请求：在聊天会话行锁内检查无活动请求，创建唯一请求、user消息、初始审计和执行任务事实。相同幂等键的竞争只产生一个请求。
5. 事件：锁请求行分配seq，插入事件并更新last_seq同事务；网络发送在事务外。终态、assistant消息、来源、审计及最后done/error事件一次提交；后续重复终态写无副作用。
6. 沉淀：消费领取可恢复；候选写入与消费done同事务；模型草稿job独立持久，重复执行不重复计频。不用最大自增ID排除晚提交日志。
7. 缺口：只有正常no_evidence/low_confidence登记outbox；以request_id去重归集。补档绑定unit/target_version，回放仍用原提问者当前合法身份，不伪造历史权限；失败不关闭。

## 4. 删除与保留

正文读取先执行墓碑检查。物理删除原文件、向量、旧事件和审计分别按已确认保留策略处理；未确定期限前不实现自动不可逆清理。删除用户以停用为首版路径，保留审计关联；组织硬删除有引用则409。备份恢复同时核验DB、原文版本和索引代际。

## 5. DDL 与迁移验收约束

编码阶段从本字典生成有版本的迁移，而不是直接复制文档建生产库。真实MySQL验证：唯一键竞争、FK删除限制、NULL/无部门去重、行锁与租约、外键索引、分页计划、事件增长与保留清理。索引名称/字段长度受目标版本限制，必须在锁定MySQL版本后验证。

若已有数据，先盘点→备份→增加兼容字段/表→回填→对照校验→切换读写；禁止同次发布直接删除旧权限字段。回滚旧程序前核对其授权语义和新事件结构，旧越权实现不作为安全回滚目标。本轮只有逻辑契约，不伪称DDL执行通过。

**生成工具链（2026-09-16 确定，M1-6）**：DDL 由 SQLAlchemy 2.0 声明式 `MetaData` 经 Alembic 生成，落在 `backend/alembic/versions/`；目标库为 **MySQL 8.0.26**（本机服务名 `MySQL80`），字符集 utf8mb4、DATETIME(6) UTC。索引与约束名由 metadata 命名约定生成。**本字典与 ORM 声明是一对一关系**：增删字段必须先改本字典再改声明；ORM 只实现本字典已列出的字段，不得自行添加"顺手"的列。集成验证需先启动 MySQL 服务，且不得以 SQLite 或元数据体检结果替代真实约束行为验证。
