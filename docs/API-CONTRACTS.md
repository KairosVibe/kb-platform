# API 与运行协议契约

版本：2026-09-16 R3。设计稿，非已实现接口。与 FUNCTION-MAP 的 F/H 编号共同使用；下方接口登记表由函数规格生成，补充入口用于闭合原有页面，不增加产品模块。

## 1. 统一协议

- 前缀 /api；JSON UTF-8。成功响应 `{code:"OK",message:"",data:...,request_id:"trace-uuid"}`，失败 `{code:"稳定错误码",message:"安全提示",data:null,request_id:"trace-uuid"}`。外层 request_id 是链路追踪 UUID，data.request_id 是问答业务正整数 ID，禁止混用。
- ID 使用正整数且不超过 JavaScript 安全整数 9007199254740991；数据库 BIGINT，生成器必须守上限。超范围 422。时间为 RFC3339 UTC，业务日/周按 Asia/Shanghai。
- GET 只有路径和 query，无 body；POST/PATCH/PUT 默认 JSON；上传为 multipart。路径字段不在 body 重复，ctx/ip/now/lease_token 从可信服务端环境获取，不接受客户端伪造。
- 创建同步资源返回 201；接受上传、问答、挖掘任务返回 202；查询/修改/删除返回 200。已存在且同载荷的幂等请求返回 200 和原 ID；同键不同载荷 409。SSE 为 text/event-stream，不套 JSON 外壳。
- PATCH 只修改提供字段，省略不变，null 仅用于允许清空的字段；JSON bool 不接受字符串或数字。未知字段拒绝 422。函数签名中的参数是合并后的服务输入，不表示 PATCH 必须传全量字段。
- revision 从 1 开始；修改 body 使用 expected_revision，DELETE 使用 `If-Match: "<revision>"`。缺失 422，不匹配 409；两个版本输入同时出现 422。服务均映射为 expected_revision。
- 默认分页 page=1,size=20，上限 100；稳定排序 created_at DESC,id DESC，部门树例外。权限过滤在分页计数前。q 最大 200 字符，title/name/category 各 200/100/100；question 1—4000，FAQ answer 1—20000，reason 1—1000。这些为首版可配置设计限制，不是模型能力声明。
- username 3—64 字符，规范化后唯一；密码不 trim、不截断，创建时 12—72 UTF-8 字节，登录允许旧短密码但拒绝 >72 字节。更换哈希算法不自动放宽此契约。source_ids/角色/ACL ID 数组去重后最多 1000 项，非法 ID 不静默丢弃。
- 401 身份无效；403 功能无权；正文、会话等按对象防枚举规则返回统一 404，不披露是否存在；管理台账最小元数据仍按 kb:view。413 超限，415 格式不支持，422 参数，409 幂等/版本/状态冲突，410 事件游标过期，429 限流，503 依赖或授权事实不可用。
- 错误码分两层，不得互相冒充：**HTTP 层**与上述状态码一一对应（4xx/5xx）；**任务层**（解析空文/加密/损坏、向量化失败等）随 `index_task.error_code` 一类 payload 返回，HTTP 仍为成功——任务是"被接受后失败"，前端据此读业务状态而非 HTTP 状态。任务层失败不得伪装成 4xx，HTTP 层错误也不得只藏在 payload 里。

## 2. 登录和功能权限例外

login/refresh 不是普通受保护路由：login 限流后校验密码与账号状态，refresh 校验刷新凭据并原子轮换；两者都不能前置 H02 功能码检查。/health 和 /ready 仅内网或受部署访问控制保护。其他入口先 H01，再按对应功能码 H02；仅登录功能的入口不凭空要求管理员角色。

Access Token 放 Authorization Bearer；refresh token 由前端受控内存保存，页面刷新后重新登录是首版行为，不落 localStorage 或 URL。Cookie 会话方案若后续引入，需要另补 CSRF 和 SameSite 契约。SSE 使用 fetch 读取流以携带 Authorization，不把 Token 写入查询参数。

权限码仍为原 14 项。手动挖掘入口需要 faq:review；定时任务使用受信执行上下文，不虚构用户 ctx。权限/账户变更由权威 DB 生效，不依赖长寿命 JWT 内的角色快照。

## 3. 上传与幂等

单文件 multipart：file、category、client_upload_id。批量：files（有序重复字段）与 manifest JSON，包含 client_batch_id、items[{client_file_id,relative_path,category}]；items 和 files 长度/顺序严格匹配。相对路径只用于展示和分组，拒绝绝对路径、盘符、.. 和 NUL；文件存储名由服务器生成。

批量响应为 batch_id、items[{client_file_id,relative_path,unit_id|null,task_id|null,error_code|null}]。有效文件可独立成功；整个请求超总大小直接 413。先前响应只含 task_id 无法对应失败文件，现补映射字段。状态查询必须返回 revision，供手动重试提交 expected_revision。单文件 client_upload_id 映射到持久 upload_item.client_file_id；批量每项使用独立UUID。

单文件默认 20 MiB、批次 100 文件/200 MiB、前端并发 3。传输进度与解析/索引进度分开。幂等键含用户与业务操作范围；文件 SHA256 + 规范化字段参与 payload_hash。网络失败后重试复用键，不对已接受文件创建第二任务。

## 4. 问答与事件

POST /api/chat/requests body={session_id,client_request_id,question}；业务唯一键(user_id,client_request_id)，payload_hash 包含 session_id 和规范化问题。accepted 是已登记，非生成成功；同一会话首版只允许一个活动请求，竞争返回409 SESSION_BUSY。

GET /api/chat/requests/{id}/events?after_seq=N；兼容 Last-Event-ID=N，若同时提供必须相同。N>=0，大于 last_seq 返回422；事件保留期外410，客户端读安全快照。连接前检查归属、登录和权限，连接后每批正文事件复核来源；不能将 HTTP 已开始后的错误写成第二个 HTTP 响应。

| event | payload | 条件 |
|---|---|---|
| meta | {request_id,session_id,status} | 首个持久事件 |
| delta | {text} | 仅授权证据生成文本 |
| citations | {items:[{no,unit_id,version,chunk_id,title}]} | 来源当前有效且可读 |
| denied | {message} | 固定安全提示，禁止受限ID/标题/数量 |
| done | {status,result_type,usage,duration_ms,first_token_ms} | 唯一业务终态已提交 |
| error | {code,message,status} | 安全错误，非原始异常 |

SSE id=seq，seq 在每个请求内递增；heartbeat 用注释帧，不持久化、不推进游标。前端完整解帧→校验seq→按幂等规则应用文本/引用/状态→提交已应用 last_seq；不得先保存游标再处理内容。缺序重新订阅，不跳过坏帧。累计回答与游标只保留内存，避免撤权后从本地持久缓存恢复敏感答案。

终态更新、最终来源、审计与 done/error 持久事件在同一 DB 事务提交，网络发送在提交之后。缺口异步工作登记 outbox，消费者以 request_id 去重；防止终态提交后进程崩溃丢失缺口。取消/失败/拒答均终态留痕，未知用量为 null。孤儿 running 请求标为 failed，不自动重启已输出生成。

撤权后不再重放旧内容；返回固定安全终止说明。该连接的安全 error 可不带事件 id，客户端不据其更新持久 seq，随后取安全快照；不得为了给一位订阅者报错而追加可泄漏旧内容的全局重放。

## 5. 原有页面缺失的补充入口

| 编号与入口 | 权限 | 输入 | 输出与逻辑 |
|---|---|---|---|
| API-S01 GET /users | sys:user | q,page,size,enabled? | items[{id,username,dept_id,role_ids,enabled,revision}],total；不返回密码哈希 |
| API-S02 GET /roles | sys:role | q,page,size | items[{id,name,codes,revision}],total |
| API-S03 GET /permission-codes | sys:role | 无 | items[{code,label,module}]；后端固定14码，前端组树 |
| API-S04 GET /knowledge-units/{id}/acl | kb:perm | 路径ID | global,depts,roles,users,revision；为编辑弹窗回填，不扩正文读权 |
| API-S05 GET /supplement-tasks | gap:handle | gap_id?,page,size | items[{id,gap_id,status,unit_id,target_version,revision}],total |
| API-S06 PUT /supplement-tasks/{id}/source | gap:handle + 来源读权 | unit_id,expected_revision | task_id,unit_id,target_version,revision；绑定当前版本，禁止绑定不存在/已删除单元 |
| API-S07 GET /mining/runs/{id} | faq:review | 路径ID | status,consumed,candidates,failed,error_code；仅统计与安全错误 |

实现命名：org_svc.list_users/list_roles/list_permission_codes、knowledge_svc.read_acl、gap_svc.list_supplement_tasks/bind_source、mining_svc.get_run。以上函数均接收服务端 ctx，依次鉴权→参数验证→有界仓储查询/条件更新→DTO；bind_source 与审计同事务，target_version 从 DB 读取不由客户端指定。调用来自对应列表、授权弹窗、补档绑定页面或轮询；不新增 US 编号，归入原 M02/M04/M06/M07。

手动 POST /mining/runs 为薄异步适配器：ctx、client_action_id、pipeline_version；验证 faq:review 和允许的 pipeline_version 后登记任务并返回202 {run_id,status}，随后内部 run_mining(run_id,trigger,pipeline_version) 执行，统计通过 API-S07 查询。trigger由服务端设为manual或schedule；任务必须复用已登记run_id，不能另建运行记录。重复相同动作复用任务，运行中重复调度不并发消费同一日志。

## 6. 功能内一致性补充

- FAQ状态 action=reject|offline|resubmit。reject 仅 candidate，需要 faq:review；offline 仅 published，需要 faq:publish；resubmit 仅 rejected/offline/stale，需要 faq:review，重新验证全部来源后转 candidate。发布必须显式走 publish。
- 模型配置读写返回 revision；embedding_model 更改不能热生效，若尚未实现索引迁移返回409 REINDEX_REQUIRED。密钥不由本接口回显或更新，部署期通过 secret_ref 管理。
- top_k 是 answer_top_k 的 API 别名；vector_top_k/keyword_top_k/max_per_route/rrf_k 在受控配置中独立，不能一字段同时修改四值。阈值绑定 score_type/model_version，禁止把 RRF 当概率。
- directory(kind=user|role|department) 仅最小选择器数据；完整用户列表用 API-S01。ACL实体选择器权限为 kb:perm，不要求额外组织管理码。
- dashboard:view 只赋予聚合统计访问。问题排行只显示调用者本人问题，跨用户只给脱敏主题/计数；知识标题仅调用者有读权时显示，否则统一“受限知识”并合并计数，不传受限ID。原始审计正文仅本人且当前来源可读。
- 无证据旧答案、FAQ来源失效和原用户停用分别返回安全占位或 blocked reason；不借管理员回放提升原提问者权限。

## 7. 兼容与验证

本平台业务 API 尚未完整实现，因此本稿为编码前契约收敛。增补返回字段、默认参数与状态 action 需同步前端 DTO 和函数规格；外层 request_id 与业务 ID 的区分必须保留。若存在未盘点客户端，实施前先对照而非直接破坏兼容。

验收覆盖每个入口：正常、401/403、防枚举404、非法参数422、版本/幂等409；写入失败不产生部分业务状态，异步失败可查询。OpenAPI 文件是下一实施阶段依据本契约生成的接口产物，当前不宣称已通过框架路由一致性验证。

## 8. 功能接口登记表

输入列为服务签名；除ctx/ip等可信参数及路径ID外，其余按GET query或写入body映射；上传和异步挖掘遵守前文专门协议。返回为data内结构。

| 功能 | HTTP入口 | 服务输入 | 输出 |
|---|---|---|---|
| F-01.01 | POST /api/auth/login | `login(username:str,password:SecretStr,ip:str)` | `{access_token:str,refresh_token:str,expires_in:int}` |
| F-01.02 | POST /api/auth/refresh | `rotate_refresh(refresh_token:SecretStr)` | `{access_token:str,refresh_token:str,expires_in:int}` |
| F-01.03 | POST /api/auth/logout | `logout(ctx:UserCtx,refresh_token:SecretStr)` | `{revoked:bool}` |
| F-01.04 | GET /api/auth/me | `get_me(ctx:UserCtx)` | `{user_id:int,username:str,dept_id:int\|null,role_ids:list[int],permission_codes:list[str],revision:int}` |
| F-02.01 | GET /api/departments | `list_departments(ctx:UserCtx)` | `{items:list[{id:int,parent_id:int\|null,name:str,revision:int}]}` |
| F-02.02 | POST /api/departments | `create_department(ctx:UserCtx,parent_id:int\|null,name:str)` | `{id:int,parent_id:int\|null,name:str,revision:int}` |
| F-02.03 | PATCH /api/departments/{id} | `update_department(ctx:UserCtx,id:int,parent_id:int\|null,name:str,expected_revision:int)` | `{id:int,revision:int}` |
| F-02.04 | DELETE /api/departments/{id} | `delete_department(ctx:UserCtx,id:int,expected_revision:int)` | `{deleted:bool}` |
| F-02.05 | POST /api/users | `create_user(ctx:UserCtx,username:str,password:SecretStr,dept_id:int\|null,role_ids:list[int])` | `{id:int,username:str,revision:int}` |
| F-02.06 | PATCH /api/users/{id} | `update_user(ctx:UserCtx,id:int,dept_id:int\|null,role_ids:list[int],enabled:bool,expected_revision:int)` | `{id:int,enabled:bool,revision:int}` |
| F-02.07 | PUT /api/roles | `save_role(ctx:UserCtx,id:int\|null,name:str,codes:list[str],expected_revision:int\|null)` | `{id:int,codes:list[str],revision:int}` |
| F-02.08 | DELETE /api/roles/{id} | `delete_role(ctx:UserCtx,id:int,expected_revision:int)` | `{deleted:bool}` |
| F-02.09 | GET /api/directory | `list_directory(ctx:UserCtx,kind:str,q:str,page:int,size:int)` | `{items:list[{id:int,label:str,enabled:bool\|null}],total:int}` |
| F-03.01 | POST /api/uploads | `accept_upload(ctx:UserCtx,file:UploadFile,category:str,client_upload_id:UUID)` | `{unit_id:int,task_id:int,status:str}` |
| F-03.02 | POST /api/upload-batches | `accept_batch(ctx:UserCtx,files:list[UploadFile],items:list[{client_file_id:UUID,relative_path:str,category:str}],client_batch_id:UUID)` | `{batch_id:int,items:list[{client_file_id:UUID,relative_path:str,unit_id:int\|null,task_id:int\|null,error_code:str\|null}]}` |
| F-03.03 | GET /api/index-tasks/{task_id} | `get_task(ctx:UserCtx,task_id:int)` | `{status:str,stage:str,progress:int\|null,error_code:str\|null,attempts:int,revision:int}` |
| F-03.05 | POST /api/index-tasks/{task_id}/retry | `retry_task(ctx:UserCtx,task_id:int,expected_revision:int)` | `{task_id:int,status:str}` |
| F-04.01 | GET /api/knowledge-units | `list_units(ctx:UserCtx,q:str,category:str\|null,enabled:bool\|null,page:int,size:int)` | `{items:list[{id:int,code:str,title:str,format:str,category:str,acl_tags:list[str],updated_at:datetime,enabled:bool,index_status:str,revision:int}],total:int}` |
| F-04.02 | PATCH /api/knowledge-units/{unit_id} | `update_metadata(ctx:UserCtx,unit_id:int,title:str,category:str,expected_revision:int)` | `{unit_id:int,revision:int}` |
| F-04.03 | GET /api/knowledge-units/{unit_id}/chunks | `read_chunks(ctx:UserCtx,unit_id:int,version:int\|null,page:int,size:int)` | `{items:list[{chunk_id:int,version:int,seq:int,text:str,page_no:int\|null,offset:int}],total:int}` |
| F-04.04 | POST /api/knowledge-units/{unit_id}/versions | `replace_document(ctx:UserCtx,unit_id:int,file:UploadFile,expected_revision:int)` | `{task_id:int,target_version:int}` |
| F-04.05 | POST /api/knowledge-units/{unit_id}/chunk-mutations | `mutate_chunks(ctx:UserCtx,unit_id:int,chunk_id:int,action:str,text:str\|null,split_offset:int\|null,expected_revision:int)` | `{task_id:int,target_version:int}` |
| F-04.06 | PUT /api/knowledge-units/{unit_id}/enabled | `set_enabled(ctx:UserCtx,unit_id:int,enabled:bool,expected_revision:int)` | `{unit_id:int,enabled:bool,revision:int}` |
| F-04.07 | DELETE /api/knowledge-units/{unit_id} | `delete_unit(ctx:UserCtx,unit_id:int,expected_revision:int)` | `{deletion_id:int,cleanup_status:str}` |
| F-04.08 | PUT /api/knowledge-units/{unit_id}/acl | `update_acl(ctx:UserCtx,unit_id:int,global:bool,depts:list[int],roles:list[int],users:list[int],expected_revision:int)` | `{acl_version:int,revision:int}` |
| F-04.09 | GET /api/acl-entities | `list_acl_entities(ctx:UserCtx,kind:str,q:str,page:int,size:int)` | `{items:list[{id:int,label:str,parent_id:int\|null}],total:int}` |
| F-05.01 | POST /api/sessions | `create_session(ctx:UserCtx,title:str\|null)` | `{session_id:int,title:str}` |
| F-05.02 | GET /api/sessions/{session_id}/messages | `read_history(ctx:UserCtx,session_id:int,page:int,size:int)` | `{items:list[{id:int,role:str,text:str\|null,restricted:bool}],total:int}` |
| F-05.03 | PATCH /api/sessions/{session_id} | `mutate_session(ctx:UserCtx,session_id:int,action:str,title:str\|null)` | `{session_id:int,deleted:bool}` |
| F-05.04 | POST /api/chat/requests | `accept_question(ctx:UserCtx,session_id:int,client_request_id:UUID,question:str)` | `{request_id:int,status:str}` |
| F-05.06 | GET /api/chat/requests/{request_id}/events | `stream_events(ctx:UserCtx,request_id:int,after_seq:int)` | `{AsyncIterator[SseEvent]}` |
| F-05.07 | POST /api/chat/requests/{request_id}/cancel | `cancel_request(ctx:UserCtx,request_id:int)` | `{request_id:int,status:str}` |
| F-05.08 | GET /api/chat/requests/{request_id}/citations/{no} | `read_citation(ctx:UserCtx,request_id:int,no:int)` | `{no:int,unit_id:int,version:int,chunk_id:int,title:str,snippet:str,page_no:int\|null,offset:int}` |
| F-05.09 | GET /api/chat/suggestions | `suggest(ctx:UserCtx,prefix:str,limit:int)` | `{items:list[str]}` |
| F-06.02 | PATCH /api/faqs/{faq_id}/candidate | `edit_candidate(ctx:UserCtx,faq_id:int,question:str,answer:str,source_ids:list[int],expected_revision:int)` | `{faq_id:int,revision:int,status:str}` |
| F-06.03 | POST /api/faqs/{faq_id}/publish | `publish(ctx:UserCtx,faq_id:int,expected_revision:int)` | `{faq_id:int,status:str,revision:int}` |
| F-06.04 | POST /api/faqs/{faq_id}/status | `change_status(ctx:UserCtx,faq_id:int,action:str,reason:str,expected_revision:int)` | `{faq_id:int,status:str}` |
| F-06.05 | PUT /api/faqs/{faq_id}/cache-enabled | `set_cache_enabled(ctx:UserCtx,faq_id:int,enabled:bool,expected_revision:int)` | `{faq_id:int,enabled:bool,status:str}` |
| F-07.02 | GET /api/knowledge-gaps | `list_gaps(ctx:UserCtx,status:str\|null,dept_id:int\|null,page:int,size:int)` | `{items:list[{id:int,question:str,dept_id:int\|null,recent_frequency:int,max_similarity:float\|null,suggested_category:str,last_seen_at:datetime,status:str}],total:int}` |
| F-07.03 | POST /api/knowledge-gaps/{gap_id}/convert | `convert_gap(ctx:UserCtx,gap_id:int,client_action_id:UUID)` | `{task_id:int,gap_id:int,status:str}` |
| F-07.04 | POST /api/knowledge-gaps/{gap_id}/verify | `verify_gap(ctx:UserCtx,gap_id:int)` | `{passed:bool,state:str,reason:str\|null}` |
| F-08.02 | GET /api/dashboard/summary | `get_summary(ctx:UserCtx,range:str,anchor_date:str)` | `{pv:int,uv:int,faq_hit_rate:float,coverage:float,knowledge_count:int,error_rate:float,unknown_usage_count:int}` |
| F-08.03 | GET /api/dashboard/charts | `get_charts(ctx:UserCtx,range:str,anchor_date:str,top_n:int)` | `{traffic:list[dict],questions:list[dict],knowledge_heat:list[dict],usage:list[dict],latency:list[dict],knowledge_counts:dict}` |
| F-08.04 | GET /api/audit | `search_audit(ctx:UserCtx,request_id:int\|null,action:str\|null,page:int,size:int)` | `{items:list[dict],total:int}` |
| F-09.01 | GET /api/model-config | `read_config(ctx:UserCtx)` | `{revision:int,models:dict,thresholds:dict,limits:dict,key_configured:bool}` |
| F-09.02 | PATCH /api/model-config | `update_config(ctx:UserCtx,patch:dict,expected_revision:int)` | `{revision:int}` |
| F-09.03 | POST /api/model-config/probe | `probe_provider(ctx:UserCtx,provider:str)` | `{ok:bool,latency_ms:int,error_code:str\|null}` |
| F-09.04 | GET /ready | `readiness(now:datetime)` | `{ready:bool,checks:dict[str,bool],version:str}` |

此外：GET /api/sessions→H28；GET /api/chat/requests/{request_id}→H27；GET /api/faqs→H34；GET /health→仅存活与版本；POST /api/mining/runs→第5节异步适配器；补充API-S01—07见第5节。内部F函数、纯前端F函数和验收函数不公开为业务API。
