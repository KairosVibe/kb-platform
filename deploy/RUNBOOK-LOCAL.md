# 本地运行手册（RUNBOOK-LOCAL）：演示 / 联调环境的启动与停止

> **范围**：开发机上的完整可运行环境（后端 API + 常驻 worker + 前端 dev + VM 中间件）。
> VM 中间件（MySQL/Milvus）的部署见 [vm/README.md](vm/README.md)；生产部署形态（docker-compose）见同目录 compose 模板。
> **本手册对应的实测环境**：Windows 开发机 + VMware 虚拟机（192.168.62.128，CentOS 7，Docker 26 + Milvus v2.5.5 standalone + MySQL 8）。

---

## 一、前置依赖（一次性）

| 项 | 说明 |
|---|---|
| VM 启动 | 承载 `MySQL:3306`（库 `kb_platform` 演示 / `kb_platform_it` 测试）与 `Milvus:19530`。**VM 若曾挂起，时钟会漂移（实测 16h）——启动后先同步时间（chrony/ntp），否则影响聚合、租约、限流** |
| Python 依赖 | `cd backend && pip install -r requirements.txt` |
| 前端依赖 | `cd frontend && npm install` |
| `backend/.env` | `DATABASE_URL`（演示库）、`MILVUS_URI`、`VECTOR_BACKEND`、`DASHSCOPE_API_KEY`（问答/向量化需要；**不入库**） |
| 演示库种子 | 首次或重置时：`cd backend; $env:KB_SEED_ADMIN_PASSWORD="<口令>"; python -m seeds.seed_initial`（种子幂等；口令只走环境变量，无默认口令） |

---

## 二、启动（4 步，按顺序，各开一个终端）

```powershell
# ① 后端 API（端口 8000）——必须 cd backend：.env 按 CWD 解析
cd d:\zcode\实战项目1\backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# ② 常驻 worker（消化索引任务 index_task 与清理任务 cleanup_task）
cd d:\zcode\实战项目1\backend
python scripts\mini_worker.py

# ③ 前端 dev（端口 5173；/api、/health、/ready 代理到 ①）
cd d:\zcode\实战项目1\frontend
npm run dev

# ④ 验证
#    http://localhost:5173            → 登录页
#    http://127.0.0.1:8000/health     → 存活探针
#    登录账号：admin（种子口令）；viewer 仅问答（ai:ask）
```

**启动顺序为什么重要**：② 依赖 ① 的数据库配置与种子数据；③ 的代理目标是 ①。① 没起时前端页面能打开但所有请求失败。

---

## 三、停止

- **前台终端**：各终端 `Ctrl+C`。
- **后台进程**（按端口/进程名清理）：

```powershell
# 按端口杀后端与前端
Get-NetTCPConnection -State Listen |
  Where-Object { $_.LocalPort -in 8000, 5173 } |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }

# 杀 mini_worker（python 进程；注意别误杀同机其他 python）
Get-Process python | Where-Object { $_.Id -ne $PID } | Stop-Process -Force
```

- **VM 中间件**：先确认没有进行中的索引/问答任务（worker 日志安静），再停 VM。**关 VM / 挂起 VM 前必须同步时钟的计划**——挂起再恢复会导致 VM 内时钟停在挂起时刻（实测漂移 16h），影响看板聚合窗口、任务租约判定与限流。

---

## 四、集成测试环境（与演示库隔离）

```powershell
# 依赖同上（VM MySQL/Milvus）；测试库 kb_platform_it（库名从 .env 的 DATABASE_URL 派生）
cd d:\zcode\实战项目1\backend
python -m pytest          # 368 项：夹具自动建表/清库；真实 DashScope/Milvus 链路的用例需要 .env 的 key
```

> 注意：pytest 连接的库与演示库是**两个库**；测试与演示互不污染。个别聚合类断言对 DB 时钟敏感（VM 时钟漂移时先同步再跑）。

---

## 五、常见问题速查

| 现象 | 原因与处置 |
|---|---|
| 登录 503"缺少配置修订快照" | 演示库没跑种子（§一第 4 步） |
| 上传成功但任务一直 `queued` 不动 | mini_worker（步骤 ②）没启动 |
| 问答受理后一直不收敛 | 同上——worker 没起，或 DashScope 配额/网络问题（看 worker 输出） |
| 集成测试整批 skip | VM 未启动 / MySQL 连不上（夹具把连不上处理成 skip） |
| 登录 401（口令确认无误） | 演示库 admin 口令与预期不符——重跑种子脚本重置 |
| 看板数字异常 / 租约类测试失败 | VM 时钟漂移 → 先同步时钟 |
| 前端页面打开但所有请求失败 | 后端（步骤 ①）没起，或端口被占用 |

---

## 六、当前实测基线（2026-09-18）

- 启动到可登录：< 30 秒（依赖已装、种子已完成）
- 上传 → 索引完成（worker 处理）：< 10 秒 / 30 条文档
- 问答（真实 DashScope 生成 + SSE）：受理到终态约 30–60 秒（思考模型）
- 验证命令：`python -m pytest`（368 passed）+ 浏览器 E2E（`node scripts/browser-e2e.mjs`，10 段 PASS）
