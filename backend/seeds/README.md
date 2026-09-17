# 种子数据与建库说明（M1-6）

依据：`DATA-CONTRACTS.md` §1/§5、`FUNCTION-MAP.md` §2.1 第 10 条。

## 1. 建库（字符集在库级设定，不在表上重复声明）

```sql
CREATE DATABASE kb_platform
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

- 库级用 `utf8mb4`：中文标题、正文、部门名都必须能存。
- 表级**不**声明字符集：`app/db/base.py` 的注释已说明原因——子类若定义元组形式的
  `__table_args__`，dialect 参数会被静默丢掉，让"看起来设置了字符集"变成假象。
- 例外是**身份类唯一列**：`user.username_norm` 与 `role.name_norm` 单独声明
  `COLLATE utf8mb4_bin`（`app/db/types.py` 的 `BINARY_COLLATION`）。库级排序规则是
  大小写不敏感的，若身份唯一性依赖它就等于把 `Admin` 和 `admin` 当成同一个人。

## 2. 迁移（唯一建表途径）

```bash
cd backend
alembic upgrade head                       # 用 app.core.config 里的 DSN
alembic -x db_url="mysql+aiomysql://..." upgrade head   # 临时覆盖 DSN
alembic upgrade head --sql > schema.sql    # 离线渲染 MySQL DDL（不连库，供审阅）
```

- DDL 只能由迁移产生。**禁止**手写生产库，也**禁止**用 `metadata.create_all()` 建库
  （该方法仅允许用于测试用的一次性库）。
- 连接串不写进 `alembic.ini`（那里留空）——DSN 含口令，进版本库等于提交凭据。
- DDL 是否通过**尚未声明**：必须在 MySQL 8.0.26 上真实执行并核对约束行为后才算验证
  （`DATA-CONTRACTS` §5 要求验证唯一键竞争、FK 删除限制、行锁、执行计划）。

## 3. 种子数据

```bash
cd backend
# 先把 MySQL80 服务启动（本机服务当前为 Stopped）
export KB_SEED_ADMIN_USERNAME=admin                     # 可选，默认 admin
export KB_SEED_ADMIN_PASSWORD='<12-72 字节的强口令>'      # 必填，无默认值
python -m seeds.seed_initial
```

写入内容：4 个演示部门、3 个角色及其功能权限码、1 个系统管理员账号、1 条配置修订。

- **不提供默认口令**：口令必须由环境变量提供，并走与登录相同的密码策略。
- **可重复执行**：已存在的记录按规范化名跳过。
- **管理员对正文默认不可见**：种子数据只授予**功能**权限码，不写入任何
  `knowledge_acl_*` 行。需要可见时必须显式配置四维授权——这是设计要求（`DESIGN_REVISION`
  §2.1"创建者和系统管理员没有自动正文读权"），不是漏配。

## 4. 执行状态（2026-09-16 已在真实 MySQL 8.0.26 上执行）

已执行并核对：

| 步骤 | 结果 |
|---|---|
| 建库 | `kb_platform` 与 `kb_platform_it`，均 utf8mb4（`information_schema` 核对） |
| 迁移 | `alembic upgrade head` 退出码 0；两库均 **37 张表**（36 业务表 + `alembic_version`），`version_num=0001` |
| 结构核对 | `user.username_norm` / `role.name_norm` = `utf8mb4_bin`；时间列 = `datetime(6)`；**13 个 CHECK** 全部创建 |
| 种子 | 退出码 0：4 部门、3 角色、**25 条角色权限码**（系统管理员 14 / 知识管理员 10 / 普通用户 1）、1 账号、1 条配置修订 |
| 安全核对 | 管理员在 `knowledge_acl_user` 中 **0 行** → "无正文读权旁路"在真实数据上成立 |

集成测试（真实 MySQL，`tests/integration/`，17 项）已通过，覆盖 CHECK 真实拦截、
FK RESTRICT/CASCADE、二进制排序规则、`DATETIME(6)` 微秒保真、跨进程重启后撤销仍生效、
并发刷新仅一个成功、`authorize_units` 端到端分桶。

仍**未**覆盖：Milvus / Neo4j 相关行为、行锁与执行计划、Docker 部署路径。
（中间件承载方式 **D-08 已于 2026-09-16 确认并由 VM 承载**，端到端往返已验证；
但 Milvus/Neo4j 的**业务行为**仍不在这套种子/集成测试的覆盖范围内。）
