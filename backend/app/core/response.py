"""统一响应包与错误码表（前后端共用契约）。

对应 FUNCTION-MAP.md §2（通用仓储与错误契约）、API-CONTRACTS.md §1、
PRD.md §1（错误码约定）。审计 PA-11 / PA-12 的修正落点。

设计要点：

1. **统一响应体**：所有响应都是 `{code, message, data, request_id}`，前端只处理一种结构。
   `request_id` 是链路追踪 UUID，与 `data.request_id`（问答业务的整数 ID）
   **是两个不同字段**，命名相同但禁止混用（API-CONTRACTS §1）。
2. **错误码分两层，不得互相冒充**（FUNCTION-MAP §2）：
   - `ERROR_CODES`：**HTTP 层**，与 401/403/404/409/410/413/415/422/429/503 一一对应；
   - `TASK_ERROR_CODES`：**任务层**，随 `index_task.error_code` 一类 payload 返回，
     此时 HTTP 仍为成功——任务是"被接受后失败"，不是"请求失败"。
   两层刻意分开：`BizError` 只接受 HTTP 层错误码，误传任务层码会在构造时立刻报错，
   从实现层面堵住"任务失败被伪装成 4xx"或反之。
3. **状态码来源**：API-CONTRACTS §1 与 PRD §1（413 大小 / 415 格式 / 422 参数 /
   409 版本与状态冲突 / 410 事件游标过期 / 429 限流 / 503 依赖不可用）。
4. 不向客户端泄漏堆栈或内部路径；详细堆栈只进服务端日志。
"""

from __future__ import annotations

from typing import Any, Generic, TypedDict, TypeVar

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import ensure_request_id, get_logger

T = TypeVar("T")


class ApiResponse(TypedDict, Generic[T]):
    """统一响应体形状。

    用 TypedDict 而非 dataclass 的原因：
    1. **运行时它就是 dict**（`ok()` 直接构造字典并由 FastAPI 序列化），声明成 dataclass
       会造成"看着像对象、实为字典"的认知错位，且 `to_dict()` 是多余的一层转换；
    2. `@dataclass(slots=True)` 与 `Generic` 组合属版本敏感用法——`slots=True` 会生成
       **新类对象**，早期 Python 版本受 `__class_getitem__` 缓存影响，参数化后可能拿回
       未应用 slots 的类；
    3. TypedDict 不参与实例化，天然无此问题，且仍能为静态检查提供字段类型。
    """

    code: str
    message: str
    data: T | None
    request_id: str


# ---------------------------------------------------------------- HTTP 层错误码
# 结构：错误码 -> (HTTP 状态, 用户可读文案)。文案直接展示给最终用户，
# 因此必须是"用户能理解的说明"而非技术术语。
ERROR_CODES: dict[str, tuple[int, str]] = {
    # ---- 认证 401 ----
    "BAD_CREDENTIALS": (status.HTTP_401_UNAUTHORIZED, "用户名或密码错误"),
    "USER_DISABLED": (status.HTTP_401_UNAUTHORIZED, "账号已被停用"),
    "TOKEN_EXPIRED": (status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录"),
    "TOKEN_INVALID": (status.HTTP_401_UNAUTHORIZED, "登录凭证无效"),
    # ---- 授权 403 ----
    "PERM_DENIED": (status.HTTP_403_FORBIDDEN, "无操作权限"),
    "PERM_AI_DENIED": (status.HTTP_403_FORBIDDEN, "未开通 AI 问答权限"),
    "FORBIDDEN_OWNER": (status.HTTP_403_FORBIDDEN, "无权操作他人的资源"),
    # ---- 资源不存在 404（按对象防枚举，不披露是否存在）----
    "NOT_FOUND": (status.HTTP_404_NOT_FOUND, "资源不存在或无权访问"),
    # ---- 载荷与格式 413 / 415 ----
    "FILE_TOO_LARGE": (status.HTTP_413_CONTENT_TOO_LARGE, "文件超过 20MB 限制"),
    "UNSUPPORTED_FORMAT": (
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "不支持的文件格式（当前支持 DOCX / 文本 PDF / Markdown / TXT）",
    ),
    # ---- 参数 422 ----
    "INVALID_ARGUMENT": (status.HTTP_422_UNPROCESSABLE_CONTENT, "请求参数有误"),
    "INVALID_PERM_CODE": (status.HTTP_422_UNPROCESSABLE_CONTENT, "包含未注册的权限码"),
    "EMPTY_FILE": (status.HTTP_422_UNPROCESSABLE_CONTENT, "文件内容为空"),
    "DEPT_CYCLE": (status.HTTP_422_UNPROCESSABLE_CONTENT, "不能将部门移动到自己的下级"),
    "PASSWORD_LENGTH_INVALID": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "密码长度不符合要求（12—72 个字节）",
    ),
    # ---- 冲突 409（版本 / 幂等 / 状态）----
    "REVISION_CONFLICT": (status.HTTP_409_CONFLICT, "数据已被他人修改，请刷新后重试"),
    "DEPT_IN_USE": (
        status.HTTP_409_CONFLICT,
        "该部门下存在子部门、用户或授权引用，无法删除",
    ),
    "DEPT_DUPLICATE": (status.HTTP_409_CONFLICT, "同级已存在同名部门"),
    "ROLE_IN_USE": (
        status.HTTP_409_CONFLICT,
        "该角色已分配给用户或被授权引用，无法删除",
    ),
    "ROLE_PROTECTED": (status.HTTP_409_CONFLICT, "不能裁剪或删除最后一个管理能力"),
    "USER_DUPLICATE": (status.HTTP_409_CONFLICT, "用户名已存在"),
    "SELF_LOCK": (status.HTTP_409_CONFLICT, "不能停用或删除最后一个系统管理账号"),
    "SESSION_BUSY": (status.HTTP_409_CONFLICT, "该会话已有进行中的提问"),
    "REINDEX_REQUIRED": (
        status.HTTP_409_CONFLICT,
        "embedding 模型已变更，需重建索引后才能生效",
    ),
    "FAQ_SOURCE_MISSING": (
        status.HTTP_409_CONFLICT,
        "来源知识单元已删除或变更，请先复核来源",
    ),
    "MINING_RUNNING": (status.HTTP_409_CONFLICT, "挖掘任务正在执行中，请稍后再试"),
    # ---- 事件游标过期 410 ----
    "EVENT_CURSOR_EXPIRED": (
        status.HTTP_410_GONE,
        "事件保留期已过，请读取安全快照",
    ),
    # ---- 限流 429 ----
    "RATE_LIMITED": (status.HTTP_429_TOO_MANY_REQUESTS, "操作过于频繁，请稍后再试"),
    # ---- 依赖不可用 503 ----
    "DEPENDENCY_UNAVAILABLE": (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "依赖服务或授权事实暂不可用，请稍后重试",
    ),
    # ---- 通用 500 ----
    "INTERNAL_ERROR": (status.HTTP_500_INTERNAL_SERVER_ERROR, "服务内部错误"),
}

# ---------------------------------------------------------------- 任务层错误码
# 这些码**不是 HTTP 错误**：随 index_task.error_code 等 payload 返回，HTTP 仍为成功
# （FUNCTION-MAP F-03.03/F-03.04 的输出字段；FORMAT-ACCEPTANCE §1"损坏/加密/不支持格式
# 返回可操作原因，不静默成功"）。前端据此显示任务失败原因，而不是把 HTTP 当成功忽略。
TASK_ERROR_CODES: dict[str, str] = {
    "PARSE_EMPTY_TEXT": "文档未提取到有效文本，可能为扫描件，当前版本不支持 OCR",
    "PARSE_ENCRYPTED": "文档已加密，无法解析",
    "PARSE_CORRUPTED": "文档已损坏，无法解析",
    "PARSE_FAILED": "文档解析失败",
    "EMBED_FAILED": "向量化失败，已进入自动重试队列",
}


# ---------------------------------------------------------------- 响应构造


def ok(data: Any = None, message: str = "OK") -> ApiResponse[Any]:
    """成功响应。路由层统一 `return ok(...)`，由 FastAPI 序列化。"""
    return {
        "code": "OK",
        "message": message,
        "data": data,
        "request_id": ensure_request_id(),
    }


def error_body(code: str, message: str | None = None, data: Any = None) -> dict[str, Any]:
    """构造 HTTP 层错误响应体。

    统一入口，保证"每个响应都带 request_id"这条契约不会被某个 handler 漏掉。
    未知码一律降级为 `INTERNAL_ERROR`，不把内部/任务层错误码漏给客户端。
    """
    entry = ERROR_CODES.get(code)
    if entry is None:
        code = "INTERNAL_ERROR"
        entry = ERROR_CODES["INTERNAL_ERROR"]
        message = None
    return {
        "code": code,
        "message": message or entry[1],
        "data": data,
        "request_id": ensure_request_id(),
    }


class BizError(Exception):
    """业务异常。抛出后由 exception_handler 转成统一响应包。

    路由层不捕获业务异常，全部交给全局处理器，保证错误响应格式一致。

    ★ 只接受 **HTTP 层**错误码：任务层码（`TASK_ERROR_CODES`）应随 payload 返回，
      误传时在此**立刻抛 ValueError**——把"两层混用"从"运行时难查"变成"构造即失败"。
    """

    def __init__(self, code: str, message: str | None = None, *, data: Any = None) -> None:
        if code not in ERROR_CODES:
            hint = ""
            if code in TASK_ERROR_CODES:
                hint = "；这是任务层错误码，应随 index_task.error_code 返回，不要抛成 HTTP 错误"
            raise ValueError(f"未知的 HTTP 错误码: {code!r}{hint}")
        self.code = code
        # 允许调用方覆盖文案（用于补充上下文，如"该部门下存在 3 个用户"）
        self.message = message or ERROR_CODES[code][1]
        self.data = data
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return ERROR_CODES[self.code][0]


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理，保证任何错误都返回统一响应包。

    注意：不向客户端泄漏堆栈或内部路径（安全要求），详细堆栈只进服务端日志。
    """

    @app.exception_handler(BizError)
    async def _biz_error_handler(_: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=error_body(exc.code, exc.message, exc.data),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 提取首个字段错误，给出可读提示；不外泄完整校验上下文。
        # 参数校验失败属于"422 参数"（API-CONTRACTS §1），不是 400。
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", [])[1:]) or "参数"
        return JSONResponse(
            status_code=ERROR_CODES["INVALID_ARGUMENT"][0],
            content=error_body("INVALID_ARGUMENT", f"{loc}: {first.get('msg', '格式有误')}"),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        get_logger(__name__).exception("未捕获异常: %s", exc)
        return JSONResponse(
            status_code=ERROR_CODES["INTERNAL_ERROR"][0],
            content=error_body("INTERNAL_ERROR"),
        )
