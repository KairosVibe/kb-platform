"""协作取消信号（FUNCTION-MAP §1 `CancelSignal`）。

取消是**协作式**的：`cancel_request` 只落库标记（原子、跨进程可见），
生成循环在安全点（模型 delta 之间）轮询该标记并自行终止——
不强行杀任务，保证"已生成部分可留痕、终态可提交"。

`reason` 只进受控审计与终态记录，不下发客户端。
"""

from __future__ import annotations

from typing import Callable


class CancelSignal:
    """可协作取消对象。`check()` 由调用方注入（通常是"查 DB 标记"的节流包装）。"""

    def __init__(self, check: Callable[[], bool]) -> None:
        self._check = check
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        if self._check():
            self.reason = self.reason or "user_cancelled"
            return True
        return False
