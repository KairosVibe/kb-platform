# 原项目基线验证记录

日期：2026-09-16（B1 复验同日）
对象：`General-PurposeRAG` 来源快照
执行原则：模块门禁；当前模块通过后才进入下一模块。

## B1 最小环境与启动基线

状态：**PASSED（2026-09-16 复验通过）** ← 原记录为 BLOCKED，见 §B1-历史。

### 验收条件与结果

| # | 验收条件 | 结果 | 证据 |
|---|---|---|---|
| 1 | `uv sync --no-dev --frozen` 按 `uv.lock` 完成生产依赖安装 | ✅ 退出码 0 | 已安装 **210** 个发行包；此前报错的 `cv2` / `sqlalchemy` / `cycler` / `torch` 均已就位 |
| 2 | `npm ci` 按 `package-lock.json` 完成前端依赖安装 | ✅ 退出码 0 | Node v22.22.3、npm 12.0.2、registry `registry.npmjs.org`；3 个漏洞（1 moderate / 2 high），2 个 postinstall 被 npm 12 拦截（见 §B1-环境适配） |
| 3 | `npm run build` 完成类型检查和前端构建 | ✅ 退出码 0 | 构建耗时 31.64s，产出 `dist/`（`index.html` 0.83 kB、`index-*.js` 206.49 kB、各路由分片） |
| 4 | 后端应用可以导入并在模拟配置下启动 | ✅ | `.venv\Scripts\python.exe -c "import src.api.main"` → `IMPORT_OK app_type=FastAPI`；uvicorn 成功监听 `127.0.0.1:8123` |
| 5 | `GET /health` 返回结构化响应，且不调用云端模型 | ✅ HTTP 200 | `{"status":"ok","version":"0.2.0","components":{"milvus":"down","neo4j":"down","llm":"ok"}}` |

### 执行方式：隔离工作副本

按复用纪律，**来源快照 `General-PurposeRAG/` 未做任何修改**；验证在隔离副本中进行：

- 副本路径：`baseline-b1/`（robocopy `/E`，排除 `.venv`、`.uv-cache`、`node_modules`、`__pycache__`、`models`、`rag_storage`、`.git`、`.mypy_cache`、`.ruff_cache`）
- 副本与快照一致性的锚点：`uv.lock` SHA256 = `DC3D0AE7F28DAED8A4A45376886AAA744337D7BD3F51B051DDDA7F14AAA50024`，**两边相同**；且 `uv sync --frozen` 执行后该哈希未变 → 锁定集合未被改动
- 文件数：快照与副本均为 226（含隐藏文件）

### 副本内的两处偏差（必须记录，均为 B1 通过所需）

1. **新增 `README.md`（569 字节占位文件）**。快照不含任何 README，而 `pyproject.toml` 声明 `readme = "README.md"`，hatchling 因此报错。占位文件首行即声明"非项目文档、不得作为快照内容依据"。选择补文件而非改 `pyproject.toml`，是为了让携带依赖语义的文件与快照保持逐字节一致。
2. **`.env` 重命名为 `.env.b1-disabled`**。快照 `.env` 含真实云端凭据（见 §B1-发现），B1 要求"启动探针必须显式设置空的模型凭据"。停用后应用进入 mock 模式，`components.llm=ok` 且未发起任何云端调用。此外探针显式设置了 `LLM_API_KEY` / `EMBEDDING_API_KEY` / `DASHSCOPE_API_KEY` 为空串。

### 根因更正：不是镜像阻塞

原记录把 B1 阻塞归因于"依赖镜像传输链路阻塞"（清华镜像 TLS handshake EOF）。复验推翻该结论：

| 探测项 | 结果 |
|---|---|
| 5 个镜像的 `HEAD /simple`（清华 / PyPI 官方 / 阿里云 / 中科大 / 腾讯云） | 全部返回 HTTP 301，**TLS 握手正常** |
| 清华镜像 `opencv-python` 索引页 | HTTP 200，`Content-Length: 352704`（服务正常） |
| 清华镜像制品下载 `cycler-0.12.1-py3-none-any.whl` | HTTP 200，8321 字节，0.49s（**制品路径可用**） |
| 未修补副本首次 `uv sync --no-dev --frozen` 的真实报错 | `OSError: Readme file does not exist: README.md`（hatchling `validate_fields`） |
| 快照中任意 README 变体（`*readme*`、`*.md`，含隐藏） | **零个** |

结论：`uv sync` 在**构建本地项目元数据**阶段即中止，根本没走到依赖下载；此前观察到的 TLS EOF 属**瞬时故障**，本次在同一镜像上完整下载成功。原判断把"瞬时网络抖动"误当成"稳定根因"，并因此附带得出"锁文件正确性未验证"——而实际上 `uv.lock` 从未被质疑成功，它现在已被证明可用。

### 发现：快照缺陷与风险

| # | 发现 | 影响 |
|---|---|---|
| 1 | **快照不完整**：`pyproject.toml` 引用的 `README.md` / `README_EN.md` 均不存在 | 快照开箱即无法构建；说明该快照并非完整工程拷贝。"有源码"与"可构建"是两件事 |
| 2 | **快照 `.env` 含真实凭据**：`LLM_API_KEY`(35)、`MINERU_API_KEY`(51)、`TEXT_RERANK_API_KEY`(35)、`MILVUS_URI`(22)、`TEXT_RERANK_BASE_URL`(37) 等 11 个非空键 | 凭据管理风险：快照及其任何副本均不得外传；建议轮换并改用 `.env.example` 占位。本次仅输出键名与长度，未输出任何值 |
| 3 | 快照内已存在首次 B1 失败留下的**部分 `.venv`** | 快照已不再是"零修改副本"；该目录未被复制进 `baseline-b1` |
| 4 | 原项目日志实现缺陷：`{"ts": "i38o", "lvl": "WARNING", "mod": "deps", "msg": "Milvus 连通性探测失败（%s）: %s"}` | 时间戳字段值异常、`%s` 未被插值 → 日志不可用于事后追溯。这直接影响 R12（会话审计）的复用结论：**其日志实现不可直接继承** |
| 5 | 被推翻的假设：**中文路径对前端工具链是雷区** | 本次 `npm ci` 与 `npm run build` 均在含中文的路径（`d:\zcode\实战项目1\baseline-b1`）下成功。仅为单次证据，不构成普遍结论，但不应继续作为既定风险引用 |

### B1 环境适配（非项目缺陷）

npm 12 默认拦截未被 `allowScripts` 覆盖的 postinstall：`esbuild@0.21.5`、`vue-demi@0.14.10`。本次 `npm run build` 仍成功（esbuild 平台二进制由可选依赖包提供）。若后续出现二进制缺失，处理方式为 `npm install-scripts approve esbuild`。这是**工具链版本策略差异**，不是原项目问题。

## B1-历史：首次验证（BLOCKED，保留原文以追溯）

状态：**BLOCKED（未通过，不进入 B2）**

验收条件同上（5 项）。

环境事实：项目 `.python-version` 为 3.12、`requires-python` 为 `>=3.10`；本机 Python 3.12.7，另有 uv 管理的 3.11.15/3.12.13；Node 22.22.3、npm 12.0.2、uv 0.11.18；本机未安装 Docker，Docker Compose 路径不可验证；`General-PurposeRAG` 不是 Git 仓库，无法用 commit 标识来源；项目存在 `uv.lock`、`package-lock.json` 与 `.env`（首次未输出 `.env` 内容，未调用云端模型）；`uv.toml` 将 Python 索引固定为清华 PyPI 镜像。

| 序号 | 命令 | 退出码 | 结果 |
|---|---|---:|---|
| 1 | `uv sync --no-dev --frozen` | 1 | 下载 `opencv-python==4.11.0.86` 时，清华镜像 TLS handshake EOF |
| 2 | `uv sync --no-dev --frozen` | 1 | 重试后下载 `sqlalchemy==2.0.52` 时，同一 TLS handshake EOF |
| 3 | `uv sync --no-dev --frozen --default-index https://pypi.org/simple` | 1 | 锁定制品仍解析到清华镜像；下载 `cycler==0.12.1` 时同一 TLS handshake EOF |

当时的结论与边界：证据支持"依赖镜像传输链路阻塞"，不支持"代码不可运行"或"锁文件正确"；依赖安装未完成，后端导入、启动、健康检查与原链路均未执行；前端安装与构建未执行；未回退到非冻结解析，避免在未审查的情况下更新锁文件。

**复验更正**：上述"镜像阻塞"结论不成立（见 §B1-根因更正）；"锁文件正确性未验证"这一保留项现已验证通过（哈希未变 + 冻结安装成功）。首次记录中"避免更新锁文件"的克制是对的——正因未改动锁文件，本次复验才具有可比性。

## 结论与边界

- B1 五项验收**全部通过**，原项目在本机具备可运行基线，**可以进入 B2**（链路验证）。
- 通过的语义有边界：依赖来自公共索引的冻结锁文件；中间件（Milvus / Neo4j）未部署，`/health` 将其记为 `down` 组件状态而非启动失败；模型能力为 mock 模式，**未验证真实模型调用**。
- 未验证：真实云端模型输出质量、原项目业务链路正确性。
- **2026-09-16 补充状态**：Docker Compose 路径、真实 Milvus/Neo4j 读写与版本复核**已验证**——VM `192.168.62.128` 上 Milvus v2.5.5 + Neo4j 5.21.0（Kernel）端到端往返通过（向量建表→insert→search 命中自身；Cypher 写读一致），详见 [部署](DEPLOYMENT.md) §9.1 与 `deploy/vm/README.md`。
- 快照存在缺陷（缺 README）与风险（含真实凭据），**B1 通过不等于快照可直接作为交付物**。

## 下一步（B2 起）

1. **B2 依赖链路验证**：在 `baseline-b1/` 内验证 Milvus / Neo4j 可达性、模型适配层接口形状、解析与切片行为（需先解决本机无 Docker 的中间件部署方式）。
2. **B3 可切割性验证**：按 REUSE-MATRIX 的 R01—R15 逐项确认"可抽离"的边界，重点是 R11（身份权限事实源）与 R12（会话审计持久层）。
3. **路线决策**：完成 B2/B3 后再在"沿原项目渐进改造"与"新骨架迁入组件"之间选择；`ADR-0004` 保持开放。
4. **快照治理**：轮换 `.env` 中的真实凭据；清理快照内遗留的部分 `.venv`；如需完整快照，补充 `README.md` / `README_EN.md` 并在快照清单中记录哈希。

## 安全与兼容性观察

- 启动脚本从现有 `.env` 加载值且不覆盖已存在环境变量。后续启动探针必须显式设置空的模型凭据；本次进一步用"停用 `.env`"保证确定性，避免依赖加载器的优先级细节。
- `/health` 会探测本机 Milvus 与 Neo4j，但不调用云端模型；探测失败记录为组件状态，不等同于应用启动失败——本次实测与该约定一致。
- 原项目日志的时间戳与参数插值存在问题（§B1-发现 #4），任何以日志为证据的结论都需先确认该缺陷是否仍存在。
