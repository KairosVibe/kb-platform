# VM 中间件部署（Milvus standalone + Neo4j）

依据 `docs/DEPLOYMENT.md` §9.1 与 `docs/FORMAT-ACCEPTANCE.md` 参数表 **D-08**。

> **2026-09-16 重要更正**：本文件此前假设"镜像需从网络拉取、VM 出网须经反向隧道"。
> 实地盘点推翻了该假设——**VM 上早已导入全套离线镜像并有正在运行的 compose 项目**，
> 出网根本不是部署前置。详见 §2、§3 与 §8 变更记录。

## 1. 目标机与已核实事实（2026-09-16 实测）

| 项 | 值 |
|---|---|
| 主机 | 本地 VMware 虚拟机 `192.168.62.128`（root） |
| 系统 | CentOS Linux 7 (Core) x86_64 |
| 资源 | 4 核 / **3.7GB 内存**（实测 total 3770MB）/ 磁盘可用 17GB / swap 3.9GB |
| 容器运行时 | **Docker 26.1.4 + Docker Compose v2.27.1（已安装、已运行）** |
| 防火墙 | `firewalld` **inactive**（端口无需放行，但这也意味着中间件对本网段可见） |
| SELinux | **Enforcing** |
| 内核参数 | 已设 `vm.max_map_count=2000000`（原值 65530；**Milvus 低于该值会启动失败**） |

**中间件版本（本机离线镜像包内的实际坐标）**：

| 组件 | 镜像 | 说明 |
|---|---|---|
| Milvus | `milvusdb/milvus:v2.5.5` | standalone |
| etcd | `quay.io/coreos/etcd:v3.5.18` | Milvus 元数据 |
| MinIO | `quay.io/minio/minio:RELEASE.2024-12-18T13-15-44Z` | Milvus 对象存储 |
| Neo4j | `neo4j:5.21.0` | 图召回（实测 Kernel 5.21.0） |
| Attu | `zilliz/attu:v2.5.10` | Milvus Web GUI |

> 早期候选版本 `milvusdb/milvus:v2.4.15`、`neo4j:5.26-community` **未采用**：
> 前者后来虽拉取成功，但本机既有部署用的是 v2.5.5；后者本机无镜像且非必要。
> **不要为了"对上文档版本"而降级既有 Milvus**——那会让已有 collection 面临兼容风险。

## 2. 镜像与出网（此前判断为阻塞，实为**非阻塞**）

### 2.1 镜像**不走网络拉取**

VM 上早已具备完整镜像集，且离线包留存于 `/opt/software/`（共 3.4GB）：

```
attu.tar  milvus.tar  milvus-etcd.tar  milvus-minio.tar  minio.tar  mongo.tar
```

`docker images` 实测已有：`milvusdb/milvus:v2.5.5`、`quay.io/coreos/etcd:v3.5.18`、
`quay.io/minio/minio:RELEASE.2024-12-18T13-15-44Z`、`neo4j:5.21.0`、`zilliz/attu:v2.5.10`、
`mongo:latest`、`nginx:latest` 等 11 个。

**结论：本项目部署不需要拉任何镜像。** 出网链路即使完全不通，也不影响中间件运行。

### 2.2 出网链路（仅后续拉新镜像时才需要）

VM 内 Docker 出口配置在 `/etc/systemd/system/docker.service.d/http-proxy.conf`。

- **原值 `http://127.0.0.1:7897` 在 VM 内并无监听**——既非本地代理，也从未有隧道接上，实测不可用。
- **2026-09-16 已改为** `http://192.168.62.1:7890`（宿主机在 VMnet8 上的地址）。
  宿主机 FlClash 监听 `0.0.0.0:7890`（不是仅 localhost），故 VM 可**直连**。
- **不需要反向隧道**。直连优于隧道：不依赖 SSH 会话存活、重启 VM 后仍有效。
- `NO_PROXY=localhost,127.0.0.1,192.168.62.0/24,docker.m.daocloud.io,mirrors.aliyun.com`
  （镜像源与本地网段直连，减少对代理的依赖）。

实测证据（从 VM 内执行）：

| 探测 | 结果 |
|---|---|
| `curl -x http://192.168.62.1:7890 https://registry-1.docker.io/v2/` | **401**（可达） |
| `curl -x http://192.168.62.1:7890 https://auth.docker.io/token` | **200** |
| `curl https://docker.m.daocloud.io/v2/`（不走代理，镜像源） | **401** |
| `docker pull busybox:latest` | 成功 |

> **别把"镜像名写错"当成"网络不通"**：曾观察到 `bitnami/etcd:3.5` 与
> `minio/minio:RELEASE.2023-03-20T20-16-18Z` 拉取失败，报 `manifest not found` 与
> `pull access denied`——这两条**都是镜像坐标错误**（正确坐标见 §1），与链路无关。
> 区分办法：若 `busybox` 能拉下来，链路就是通的。

## 3. 启动（**复用既有项目，不要新建目录**）

VM 上已有一个**实际运行过约 24 天**的 compose 项目：

```
项目名 rag  →  /root/rag/docker-compose.yml（5 容器：etcd/minio/milvus/neo4j/attu）
项目名 software → /opt/software/docker-compose.yml（4 容器，纯 Milvus，无 Neo4j，属更早的演练）
```

**以 `/root/rag/` 为准**——它是唯一同时包含 Milvus + Neo4j（本项目两项都要）的部署，
且数据在 `./volumes/` 下（**已有历史数据，不要换成 named volume**）。

```bash
cd /root/rag
docker compose up -d          # 五容器合计约 1.6GB，实测可一次性起
docker compose ps             # 期望：etcd/minio/milvus healthy，neo4j 稍后 healthy
```

> 本仓库 `docker-compose.yml` 是该文件的**对齐版本**（加了来源说明与约束注解）。
> 两者在服务定义上一致；**VM 上跑的是 `/root/rag/docker-compose.yml`**。
> 若两者出现分歧，以 VM 实况为准并回写本仓库——不要让仓库声明与实况漂移。

内存说明：五容器实占约 1.6GB，整机 3.7GB + 3.9GB swap 下可行（已验证）。
若后续加容器或调大 heap，**建议把 VM 内存提到 8GB**（宿主机有 31.8GB）。

## 4. 验证

### 4.1 端口与存活（宿主机 Windows 执行）

```powershell
Test-NetConnection 192.168.62.128 -Port 19530   # Milvus gRPC
Test-NetConnection 192.168.62.128 -Port 7687    # Neo4j bolt
curl.exe http://192.168.62.128:7474             # Neo4j HTTP（应 200）
curl.exe http://192.168.62.128:18080            # Attu（应 200）
```

**⚠️ `9091` 不要从宿主机探测**：compose 有意只映射 `19530`，9091 仅在容器网络内供
healthcheck 使用，宿主机访问会被拒——**这是预期，不是故障**。Milvus 健康状态看
`docker compose ps`。同理 MinIO 控制台（19081）未对外发布。

### 4.2 端到端读写往返（**只有它能证明"可用"**）

**2026-09-16 已执行并通过**（宿主机 `pymilvus 2.6.6` / `neo4j 6.2.0` / `grpcio 1.76.0`）：

| 验证 | 结果 |
|---|---|
| Milvus：建 1024 维 collection → insert 5 条 → search top3 | ✅ 最近邻命中自身，`distance=1.0000`；用后已 `drop_collection` |
| Milvus：既有 collection 列表 | 18 个（`acme_kb_a_chunks`、`demo_kb_demo_chunks`、`fin_chunks`、`it_tenant_*` 等，含集成测试产物） |
| Neo4j：`verify_connectivity` + Cypher 写入读回 | ✅ 值一致；写读用探针节点已 `DELETE` |
| Neo4j 服务端版本 | Kernel **5.21.0** |

探针使用独立命名（`probe_m16_roundtrip` / `M16RoundtripProbe`），验证后清理，**未触碰既有数据**。

> 端口开着 ≠ 就绪。本节的往返才是就绪证据。

## 5. 后端如何连接

在 `backend/.env`（不进版本库）中设置：

```
MILVUS_URI=http://192.168.62.128:19530
NEO4J_URI=bolt://192.168.62.128:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<与 VM 上 /root/rag/.env 一致>
```

**2026-09-16 更新**：`app/core/config.py` 的默认形态已改为 `milvus_standalone`，且
"形态与地址必须一致"**已固化为启动期校验**——只改一个会在启动时直接失败，
不再需要靠人记住这条提醒。`backend/.env` 已按上文的值配置，并用后端自身的 `Settings`
实测连通本 VM 的 Milvus 与 Neo4j。配置的权威声明见 `docs/DEPLOYMENT.md` §9.2。

## 6. 未验证事项（诚实边界）

- 内存压力下的**长时间稳定性**（3.7GB 是本部署最大的不确定项；已验证可启动与往返，未验证长时间高负载）；
- 本项目**业务链路**对中间件的实际使用（写入真实切片、ACL 标量过滤下推、图召回）——§4.2 只证明了中间件本身可用；
- VM 重启后容器是否自动恢复：`/root/rag/docker-compose.yml` **未设 `restart:` 策略**，
  VM 重启后需手动 `docker compose up -d`。

## 7. 加固建议（**尚未在 VM 落地**）

以下改动会改动正在运行的部署，故仅记录、未实施：

1. 给全部服务加 `restart: unless-stopped`——当前重启 VM 后中间件不会自起。
2. `NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:?...}` 加 `:?` 断言——当前 `.env` 缺失会以空口令启动而非报错。
3. MinIO 的 `MINIO_ROOT_USER/PASSWORD` 目前是 `minioadmin/minioadmin`（compose 内明文默认值）。
   仅限实验环境；若要长期使用应改为运行时注入。
4. `9091` 是否对外发布：供宿主侧健康探测用，但会多开一个端口；当前选择不发布。

## 8. 变更记录

**2026-09-16**

- 纠正 VM 的 Docker 代理：`127.0.0.1:7897`（无监听）→ `192.168.62.1:7890`（实测可用）；
  确认**无需反向隧道**。
- **事故与恢复**：为改代理执行了 `systemctl restart docker`，该动作**停掉了原本已运行约 24 天的
  五个容器**（容器无 `restart` 策略，daemon 重启即停；退出码 255、`OOMKilled=false`）。
  已用 `cd /root/rag && docker compose up -d` 恢复，五容器重回 Up（etcd/minio/milvus healthy）。
  **教训：在有运行中部署的机器上重启 docker daemon 前，先确认容器重启策略。**
- 盘点发现既有离线镜像与既有 compose，据此**放弃**"拉镜像"路线；本目录 compose 改为对齐 VM 实况。
- 端到端往返验证通过（§4.2）。
