"""任务层错误（FUNCTION-MAP §2"错误码分两层"的任务层一侧）。

★ 为什么不放 `app/core/response.py`：那里是 **HTTP 层**（BizError 与状态码一一对应）。
  任务层错误（解析空文/加密/损坏、向量化失败等）随 `index_task.error_code` 一类 payload
  返回，**HTTP 仍为成功**——把任务层错误抛成 BizError 会把"任务失败"伪装成"请求失败"，
  正是 FUNCTION-MAP §2 明文禁止的互串。

`transient` 决定任务的终态走向（F-03.04）：
- `True`（瞬时：网络抖动、限流、依赖暂不可用）→ `retry_wait`，由 H14 到期重试；
- `False`（永久：解析空文/加密/损坏、维度不匹配、版本已过时）→ `failed`，不循环。

★ 任务层错误码**只存在于代码**，不写进契约文档（门禁会把契约文档里的
  UPPER_SNAKE 记号当作必须实现的 HTTP 错误码核对——两层码不能混写）。
"""

from __future__ import annotations


class TaskError(Exception):
    """任务层失败。`code` 写入 `index_task.error_code`；`transient` 决定重试还是终结。"""

    def __init__(self, code: str, message: str = "", *, transient: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.transient = transient
