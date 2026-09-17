"""字段长度、排序规则与类型约定。

依据：DATA-CONTRACTS.md §1（"状态字段 VARCHAR(32)"、"文本 utf8mb4，用户名和幂等键采用
明确规范化/二进制比较，禁止依赖默认排序规则决定安全身份"）与 §5（"索引名称/字段长度受
目标版本限制，必须在锁定 MySQL 版本后验证"）。

**这里只是把散落在 40 张表里的同类取值收敛到一处**，不是新契约。长度取值约束：

- 索引列一律不超过 `VARCHAR(255)`：utf8mb4 下 255×4 = 1020 字节，在 InnoDB DYNAMIC
  行格式的 3072 字节索引上限内有充足余量；
- 需要唯一性判定的身份类字段用**二进制排序规则**（`utf8mb4_bin`），而不是依赖数据库
  默认排序规则：默认的 `utf8mb4_general_ci` / `utf8mb4_0900_ai_ci` 会把大小写甚至变音
  视为相等，从而让"Admin"与"admin"成为同一个身份——安全身份不能由排序规则决定。
"""

from __future__ import annotations

from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime

# ---------------------------------------------------------------- 时间

#: 全库统一的 UTC 时间类型：MySQL 上渲染为 `DATETIME(6)`。
#:
#: ★★ 为什么不用泛型 `DateTime(6)`：**MySQL 方言不读取泛型类型的 precision**，
#:    DDL 会退化成 `DATETIME`（秒精度），微秒被静默丢弃。这不是理论风险——
#:    初始迁移的离线 DDL 渲染实测到了（`DATETIME(6)` 出现 0 次），见 WORKLOG 问题 #38。
#:    后果不是"少几位小数"：`consumed_at`、`last_seq` 同级的时间排序会变得不确定，
#:    租约比较（lease_until）与令牌到期判断也会失去精度，而这些恰好是并发正确性的依据。
#:
#: 代价：本类型是 MySQL 方言专有，迁移脚本在 SQLite 上不可执行。
#:      这是可接受的——DATA-CONTRACTS §5 已明确目标是 MySQL 8.0.26，且禁止用
#:      SQLite 结果替代 MySQL 验证。
UTCDateTime = MySQLDateTime(fsp=6)

# ---------------------------------------------------------------- 长度

LEN_NAME = 64          # 部门名、角色名（需要唯一索引）
LEN_USERNAME = 64      # 登录名（username / username_norm，唯一索引）
LEN_CODE = 64          # 知识单元 code、权限码等业务编码
LEN_TITLE = 255        # 标题类
LEN_STATUS = 32        # 状态/阶段字段（DATA-CONTRACTS §1 明确规定 32）
LEN_HASH = 255         # bcrypt 60 字符，预留后续算法升级空间
LEN_TOKEN_HASH = 128   # 刷新令牌哈希（SHA-256 hex 为 64，留余量）
LEN_FILE_KEY = 255     # 受控存储键（UUID 路径），不是用户路径
LEN_PARSER_VERSION = 64
LEN_MODEL_VERSION = 128  # provider:model:dimension 拼串
LEN_ERROR_CODE = 64
LEN_ACTION = 64        # operation_log.action
LEN_RESOURCE_TYPE = 64
LEN_IP = 45            # IPv6 最长 45 字符（预留审计用）
LEN_IDEMPOTENCY_KEY = 128  # client_* 幂等键

# ---------------------------------------------------------------- 排序规则

#: 二进制排序规则：用于用户名、角色名等"身份判定"字段（同上注释）。
BINARY_COLLATION = "utf8mb4_bin"
