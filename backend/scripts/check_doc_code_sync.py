"""文档 ↔ 代码一致性核对（R3 锚点版）。

存在理由（WORKLOG 问题 #14）：FUNCTION-MAP 中的类型声明漏标 `slots=True`，而代码里
是有的——文档与代码不一致，且**没有任何机制能发现**。本脚本把这类核对固化为可重复动作。

★ 第二版修订原因（PERMISSION-CONTRACT-AUDIT PA-13）：
  第一版把"正则没匹配上"当作"检查通过"。R3 文档改写后（PRD 由"权限码全集（14 个）"
  改为"14个功能码：…"、DENY_* 不再出现在 PRD），三个锚点静默消失，脚本进入
  **既无法证明一致、又持续误报**的失效状态。本版针对根因做三件事：
    1. 所有依赖的文档锚点显式登记在 ANCHORS 中，匹配不到即报"锚点失效"
       （锚点失效本身是一类缺陷，而不是通过）；
    2. 引入 KNOWN_DIVERGENCES 已知差异清单——已在
       docs/PERMISSION-CONTRACT-AUDIT.md 登记且带 PA 编号的未关闭差异，
       单独列出但不计入退出码；只有**新增漂移**才返回 1。
       这不是"用白名单掩盖失败"：条目必须对应审计文档中的编号，且 PA 关闭时必须
       同步删除条目（脚本会报告不再触发的陈旧条目）；
    3. 增加"检查自身有效性"的护栏：发现 0 个待核对对象时也报错，避免静默空转。

核对项：
  1. 锚点存活：ANCHORS 中每个文档锚点必须仍能匹配；
  2. 代码 dataclass 是否都用了 slots=True；
  3. 权限码集合与数量：代码 PERMISSIONS vs DESIGN_REVISION §2.1 / PRD §1 双锚点；
  4. 文档提及的 HTTP 错误码必须在 ERROR_CODES 中定义；
  5. HTTP 状态码语义：API-CONTRACTS §1 声明的状态码须有实现，且指定映射不得错配；
  6. 客户端受限原因码契约：DESIGN_REVISION §2.2 声明的 reason_code 必须在代码中存在；
  7. 代码注释引用的文档章节与编号必须真实存在（溯源有效性）；
  8. 契约层（`app/engines|services|tasks|providers|utils`）的公开类/函数必须登记在
     FUNCTION-MAP——把"编码前先更新 FUNCTION-MAP"这条流程规则机械化。

用法：python scripts/check_doc_code_sync.py
退出码：0 无新增漂移（可能含已知未关闭项）；1 发现新增漂移或锚点失效。
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
TESTS = ROOT / "tests"
DOCS = ROOT.parent / "docs"
SELF = Path(__file__).resolve()

# 参与错误码/编号核对的 R3 契约文档（历史文档 INTERVIEW/WORKLOG/REUSE_* 不作为契约来源）
CONTRACT_DOCS = (
    "DESIGN_REVISION.md",
    "PRD.md",
    "FUNCTION-MAP.md",
    "ARCHITECTURE.md",
    "API-CONTRACTS.md",
    "DATA-CONTRACTS.md",
    "FORMAT-ACCEPTANCE.md",
)

# ---------------------------------------------------------------- 锚点登记
# 每个检查依赖的文档锚点必须在此登记。匹配不到 = 门禁失效 = 报错。
ANCHORS: dict[str, tuple[str, str]] = {
    "permission_codes@design_revision": (
        "DESIGN_REVISION.md",
        r"(\d+)\s*个权限码保留：([^。]+)",
    ),
    "permission_codes@prd": ("PRD.md", r"(\d+)个功能码：([^。]+)"),
    # 状态码语义以 API 契约为权威来源（PRD §1 的同一句未含 410）。
    # 用整行匹配而非"跨标点"匹配：该句中 404 与 413 之间本身就有句号，
    # 第一版 `401[^。]*?503` 因此匹配失败（锚点静默失效的第二次现场）。
    "http_status@api_contracts": ("API-CONTRACTS.md", r"(?m)^(?:- )?401[^\n]*503[^\n]*$"),
    "denied_contract@design_revision": (
        "DESIGN_REVISION.md",
        r"reason_code=([A-Z][A-Z_]+)",
    ),
    "branch_contract@prd": ("PRD.md", r"无创建者/超管读权旁路"),
    "dept_match@design_revision": ("DESIGN_REVISION.md", r"部门仅直属部门匹配"),
}

# ---------------------------------------------------------------- 已知差异清单
# key -> "PA 编号"。条目必须已在 docs/PERMISSION-CONTRACT-AUDIT.md 登记。
# 关闭对应 PA 时必须删除条目；脚本会提示不再触发的陈旧条目。
# `doc-ref:*` 为通配：PA-14 是整类债务（注释引用旧编号），修复时须整体重写引用并删除本条。
KNOWN_DIVERGENCES: dict[str, str] = {
    # --- PA-11 已于 2026-09-16 关闭（步骤 5）：HTTP 状态码与错误码表已按
    #     API-CONTRACTS §1 / PRD §1 对齐，原 9 条已知差异全部删除；
    #     同时把受影响的状态码与具名错误码纳入 REQUIRED_MAPPINGS 强制校验，防止回退。 ---
    # --- PA-14 已于 2026-09-16 关闭（步骤 6 前半）：全部失效的旧编号引用已改引
    #     R3 编号，原 `doc-ref:*` 通配条目已删除。 ---
    # 清单当前为空：任何新出现的文档/代码漂移都会直接让门禁返回 1。
}

# API-CONTRACTS §1 必须声明的状态码全集（少于这些说明契约句被改动，须同步本脚本）
REQUIRED_STATUSES: frozenset[int] = frozenset(
    {401, 403, 404, 409, 410, 413, 415, 422, 429, 503}
)

# 契约明确指定了具体状态码的映射（来源见注释），不得错配。
# PA-11 关闭后已扩到全部易错项：改动这些映射会让门禁直接失败。
REQUIRED_MAPPINGS: dict[str, tuple[int, str]] = {
    "UNSUPPORTED_FORMAT": (415, "API-CONTRACTS §1「415 格式不支持」"),
    "INVALID_ARGUMENT": (422, "API-CONTRACTS §1「422 参数」"),
    "INVALID_PERM_CODE": (422, "PRD AC-02.07-02「未知码422」"),
    "PASSWORD_LENGTH_INVALID": (422, "FUNCTION-MAP F-01.01「超 72 字节显式拒绝」"),
    "SELF_LOCK": (409, "PRD AC-02.06-02「保护最后一个系统管理账号；冲突409」"),
    "SESSION_BUSY": (409, "API-CONTRACTS §4「同会话并发返回409 SESSION_BUSY」"),
    "REINDEX_REQUIRED": (409, "API-CONTRACTS §6「embedding 变更返回409 REINDEX_REQUIRED」"),
    "EVENT_CURSOR_EXPIRED": (410, "API-CONTRACTS §1「410 事件游标过期」"),
    "DEPENDENCY_UNAVAILABLE": (503, "API-CONTRACTS §1「503 依赖或授权事实不可用」"),
}

# 与 HTTP 错误码同形但属其他体系/其他语义的记号，不纳入错误码核对
NON_HTTP_TOKENS: frozenset[str] = frozenset(
    {
        "ACCESS_RESTRICTED",   # 客户端受限原因码（DESIGN_REVISION §2.2）
        "HTTP_413_CONTENT_TOO_LARGE",  # 框架常量名，非本平台错误码
        "HTTP_413_REQUEST_ENTITY_TOO_LARGE",
    }
)

# 以下后缀是"展示常量/文案常量"命名，不是错误码——避免把文档里的固定提示常量
# （如 PARTIAL_RESTRICTED_NOTICE）误判为未实现的错误码。
NON_HTTP_SUFFIXES: tuple[str, ...] = ("_NOTICE", "_MESSAGE")

# 形如 `X-N` 但属标准/协议名而非文档编号，不纳入编号核对
NON_DOC_TOKENS: frozenset[str] = frozenset(
    {"UTF-8", "UTF-16", "SHA-256", "ISO-8601", "RFC3339", "HTTP-2", "TLS-1"}
)

# 与契约编号**同形**但属"业务数据命名空间"的前缀，不纳入编号核对。
#
# ★ 为什么用"前缀黑名单"而不是"契约前缀白名单"：白名单的反面是"未知前缀一律不查"，
#   那会把将来真实引入的契约编号（如刚引入的 `FR-5`）**静默跳过**——正是本脚本反复
#   栽过的"静默空转"（见文件头与 WORKLOG 问题 #18）。黑名单则相反：默认照旧全查，
#  只有在此显式登记、并写明理由的前缀才放行。
# ★ 若本项目将来真的引入 `KB-nn` 形式的契约编号，必须先删除本条目。
NON_CONTRACT_PREFIXES: frozenset[str] = frozenset(
    {
        # knowledge_unit.code 的取值（种子数据与测试夹具，如 'KB-0001'）。
        # 它是**数据**，不是对文档的引用；把它当编号核对会误报。
        "KB",
    }
)


@dataclass(frozen=True, slots=True)
class Problem:
    check: str
    key: str
    detail: str

    @property
    def allowlist_key(self) -> str:
        return f"{self.check}:{self.key}"


# ---------------------------------------------------------------- 工具


def _doc(name: str) -> str:
    path = DOCS / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _iter_code_files() -> list[Path]:
    files = [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]
    files += [p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts]
    return [p for p in files if p.resolve() != SELF]


def _parse_code_list(raw: str) -> set[str]:
    """把"a、b、c。"形式的文档列举解析为集合。"""
    return {
        token.strip("` 　。；;、")
        for token in re.split(r"[、,，]", raw)
        if token.strip("` 　。；;、")
    }


def _import_after_path_setup(module: str, attr: str) -> object | None:
    sys.path.insert(0, str(ROOT))
    try:
        return getattr(__import__(module, fromlist=[attr]), attr)
    except Exception:  # noqa: BLE001
        return None


def _heading_numbers(text: str) -> set[str]:
    """提取形如 `## 2.1 标题` 的章节编号集合。"""
    out: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"#{2,4}\s+(\d+(?:\.\d+)*)[\s.、]", line.strip())
        if m:
            out.add(m.group(1))
    return out


def _plan_decisions() -> set[str]:
    """PLAN §2 已决事项的条目序号集合。"""
    section = re.search(r"##\s*2\.\s*已决事项(.*?)(?=\n##\s)", _doc("PLAN.md"), re.S)
    if not section:
        return set()
    return set(re.findall(r"^(\d+)\.", section.group(1), re.M))


# ---------------------------------------------------------------- 检查 1：锚点存活


def check_anchors() -> tuple[list[Problem], str]:
    problems: list[Problem] = []
    for name, (doc_name, pattern) in ANCHORS.items():
        if not re.search(pattern, _doc(doc_name)):
            problems.append(
                Problem(
                    "anchor",
                    name,
                    f"锚点失效：{doc_name} 中匹配不到 /{pattern}/；"
                    "依赖此锚点的检查已失去意义，须更新锚点定义或文档",
                )
            )
    return problems, f"{len(ANCHORS) - len(problems)}/{len(ANCHORS)} 锚点存活"


# ---------------------------------------------------------------- 检查 2：代码 slots


def check_code_dataclass_slots() -> tuple[list[Problem], str]:
    """代码中每个 @dataclass 都必须带 slots=True（FUNCTION-MAP §1 统一口径）。

    注：第一版另有"文档 dataclass 声明标注 slots"一项。R3 文档已不再使用
    `@dataclass` 记法（公共类型改为 §1 的逐字段列举），该检查已无对象，故移除。
    """
    problems: list[Problem] = []
    pattern = re.compile(r"@dataclass\(([^)]*)\)\s*\nclass\s+(\w+)", re.M)
    found = 0
    for path in _iter_code_files():
        for args, cls in pattern.findall(path.read_text(encoding="utf-8")):
            found += 1
            if "slots=True" not in args:
                rel = path.relative_to(ROOT).as_posix()
                problems.append(
                    Problem("code-slots", cls, f"{rel} :: {cls} 未使用 slots=True")
                )
    if found == 0:
        problems.append(
            Problem("code-slots", "none-found", "未在代码中发现任何 @dataclass，检查可能已失效")
        )
    return problems, f"核对 {found} 个代码 dataclass"


# ---------------------------------------------------------------- 检查 3：权限码


def check_permission_codes() -> tuple[list[Problem], str]:
    problems: list[Problem] = []
    perms = _import_after_path_setup("app.core.permissions", "PERMISSIONS")
    if perms is None:
        return [Problem("perm-codes", "import", "无法导入 app.core.permissions")], "导入失败"

    code_codes = {p.code for p in perms}  # type: ignore[attr-defined]

    for anchor, doc_name in (
        ("permission_codes@design_revision", "DESIGN_REVISION.md"),
        ("permission_codes@prd", "PRD.md"),
    ):
        m = re.search(ANCHORS[anchor][1], _doc(doc_name))
        if not m:
            continue  # 锚点失效已由检查 1 报告，此处不重复
        claimed_count, raw_list = int(m.group(1)), m.group(2)
        doc_codes = _parse_code_list(raw_list)
        if claimed_count != len(code_codes):
            problems.append(
                Problem(
                    "perm-codes",
                    f"count@{doc_name}",
                    f"{doc_name} 声称 {claimed_count} 个，代码 PERMISSIONS 为 {len(code_codes)} 个",
                )
            )
        for code in sorted(code_codes - doc_codes):
            problems.append(
                Problem("perm-codes", f"missing@{doc_name}:{code}", f"{doc_name} 未列出 {code}")
            )
        for code in sorted(doc_codes - code_codes):
            problems.append(
                Problem("perm-codes", f"extra@{doc_name}:{code}", f"{doc_name} 多出未注册码 {code}")
            )
    return problems, f"与 DESIGN_REVISION / PRD 双锚点对照（{len(code_codes)} 个）"


# ---------------------------------------------------------------- 检查 4：错误码


def _declared_env_vars() -> frozenset[str]:
    """从 `DEPLOYMENT §9.2` 的变量表解析出**环境变量命名空间**。

    ★ 为什么需要它：环境变量名与 HTTP 错误码**同形**（都是 UPPER_SNAKE 带下划线），
      但属两个体系——前者是部署配置的键，后者是响应里的错误码。在一个契约文档里
      提一句 `DATABASE_URL`（例如说明"该值随承载方式变化"），就会被判成
      "文档提及错误码 DATABASE_URL，但 ERROR_CODES 未定义"，属误报。

    ★ 为什么解析而不是硬编码清单：环境变量会随部署演进增加，逐条加白名单必然漏。
      这里以 **DEPLOYMENT §9.2 的表格为唯一来源**（该节本就是"部署期环境变量的唯一
      声明处"），新增变量只要登记进那张表，本项检查自动放行——不需要两处维护。
    """
    section = re.search(r"###\s*9\.2\b(.*?)(?=\n#{2,4}\s|\Z)", _doc("DEPLOYMENT.md"), re.S)
    if not section:
        return frozenset()
    names: set[str] = set()
    for raw in section.group(1).splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        # 只看**第一个单元格**（变量名列）；一行可登记多个，用 `/` 分隔。
        names |= set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", cells[1]))
    return frozenset(names)


def check_error_code_tokens() -> tuple[list[Problem], str]:
    """文档中出现的 UPPER_SNAKE 错误码必须有实现。

    仅取"含下划线的大写下划线串"，避免把 BIGINT / DATETIME / SSE 等类型名误判为错误码；
    DENY_* 属数据权限原因码、ACCESS_RESTRICTED 属客户端受限原因码，二者与 HTTP 错误码
    是独立体系（第一版假阳性教训，见 WORKLOG 问题 #15），显式排除。

    注：不要求反引号——API-CONTRACTS 直接写 `409 SESSION_BUSY` 而不加反引号，
    第一版因强制反引号而漏检，使该检查在 R3 文档下静默空转（0 个待核对对象）。
    """
    problems: list[Problem] = []
    known = _import_after_path_setup("app.core.response", "ERROR_CODES")
    if known is None:
        return [Problem("error-token", "import", "无法导入 app.core.response")], "导入失败"

    pattern = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
    mentioned: set[str] = set()
    for doc_name in CONTRACT_DOCS:
        mentioned |= set(pattern.findall(_doc(doc_name)))
    mentioned -= NON_HTTP_TOKENS
    env_vars = _declared_env_vars()
    if not env_vars:
        problems.append(
            Problem(
                "error-token",
                "env-vars-none-found",
                "未能从 DEPLOYMENT §9.2 解析到任何环境变量名；"
                "该节是环境变量命名空间的唯一来源，解析为空会让本项检查的排除逻辑失效",
            )
        )
    mentioned = {t for t in mentioned if t not in env_vars}
    mentioned = {t for t in mentioned if not t.startswith("DENY_")}
    mentioned = {t for t in mentioned if not t.endswith(NON_HTTP_SUFFIXES)}
    # 文档名本身（DESIGN_REVISION / REUSE_NOTES …）与错误码同形，按"存在同名文档"排除
    mentioned = {t for t in mentioned if not (DOCS / f"{t}.md").exists()}

    if not mentioned:
        return (
            [Problem("error-token", "none-found", "契约文档中未解析到任何错误码，检查可能已失效")],
            "未解析到错误码",
        )

    for token in sorted(mentioned - set(known)):  # type: ignore[arg-type]
        problems.append(
            Problem("error-token", token, f"文档提及错误码 {token}，但 ERROR_CODES 未定义")
        )
    return problems, f"文档提及 {len(mentioned)} 个错误码"


# ---------------------------------------------------------------- 检查 5：状态码


def check_http_status() -> tuple[list[Problem], str]:
    problems: list[Problem] = []
    table = _import_after_path_setup("app.core.response", "ERROR_CODES")
    if table is None:
        return [Problem("http-status", "import", "无法导入 app.core.response")], "导入失败"
    assert isinstance(table, dict)

    used_statuses = {status for status, _ in table.values()}

    # 5a. 契约声明句若被改动导致状态码集合缩小，须同步本脚本（否则检查静默退化）
    m = re.search(ANCHORS["http_status@api_contracts"][1], _doc("API-CONTRACTS.md"))
    if m:
        declared = {int(s) for s in re.findall(r"(\d{3})", m.group(0))}
        for status in sorted(REQUIRED_STATUSES - declared):
            problems.append(
                Problem(
                    "http-status",
                    f"contract-drift-{status}",
                    f"契约声明句不再包含 {status}，请核对 API-CONTRACTS §1 并同步脚本",
                )
            )
        # 5b. 每个声明状态码都要有实现
        for status in sorted(declared):
            if status not in used_statuses:
                problems.append(
                    Problem(
                        "http-status",
                        f"missing-status-{status}",
                        f"API-CONTRACTS §1 声明 {status}，但 ERROR_CODES 中无任何错误码映射到该状态",
                    )
                )

    # 5c. 契约明确指定状态码的映射不得错配
    for code, (expected, origin) in REQUIRED_MAPPINGS.items():
        if code not in table:
            continue
        actual = table[code][0]
        if actual != expected:
            problems.append(
                Problem(
                    "http-status",
                    f"code-{code}",
                    f"{code} 实为 {actual}，契约要求 {expected}（{origin}）",
                )
            )
    return problems, f"已用状态码 {sorted(used_statuses)}"


# ---------------------------------------------------------------- 检查 6：受限原因码


def check_denied_contract() -> tuple[list[Problem], str]:
    """DESIGN_REVISION §2.2 声明的客户端可见原因码必须在代码中存在。

    该契约规定：客户端 denied 只含 reason_code=ACCESS_RESTRICTED 与固定 message。
    代码若未定义该原因码，则当前对外提示必然携带了不应暴露的维度信息（PA-04/PA-05）。
    """
    problems: list[Problem] = []
    pattern = ANCHORS["denied_contract@design_revision"][1]
    declared = set(re.findall(pattern, _doc("DESIGN_REVISION.md")))
    if not declared:
        return (
            [Problem("denied-contract", "none-declared", "契约文档未声明客户端可见原因码，检查可能已失效")],
            "未声明",
        )

    code_text = "\n".join(p.read_text(encoding="utf-8") for p in _iter_code_files())
    for code in sorted(declared):
        # 必须存在**字符串字面量**（枚举成员值或常量）才算定义；
        # 仅在注释里提一句不算——否则本检查会被一句说明文字骗过。
        if f'"{code}"' not in code_text and f"'{code}'" not in code_text:
            problems.append(
                Problem(
                    "denied-contract",
                    code,
                    f'契约声明客户端可见原因码 {code}，但代码中未见 "{code}" 字面量定义',
                )
            )
    return problems, f"契约声明 {sorted(declared)}"


# ---------------------------------------------------------------- 检查 7：文档引用


_SECTION_REF = re.compile(r"([A-Z][A-Z0-9_-]+)(?:\.md)?\s*§\s*([\d.]+)")
_PLAN_REF = re.compile(r"PLAN(?:\.md)?\s*决策\s*#(\d+)")
_NUMBER_REF = re.compile(r"\b([A-Z]{1,4}-\d+(?:\.\d+)*)\b")
# 审计编号（PA-nn）指向 docs/PERMISSION-CONTRACT-AUDIT.md，**单独校验命名空间**：
# 若把审计文档混入契约文档语料，则 PA-14 里那些失效的旧编号会因"审计文档提到过"而被洗白。
_AUDIT_TOKEN = re.compile(r"PA-\d{2}")


def check_doc_references() -> tuple[list[Problem], str]:
    """代码注释声明的出处必须真实存在于对应文档，否则评审无法回溯（PA-14）。

    三类引用分别核对：章节引用（`X.md §n`）按目标文档的标题编号校验；
    契约编号（`FR-5` / `BC-5.2.4` / `NFR-7` / `C-4`）按是否出现在任一契约文档中校验；
    审计编号（`PA-nn`）按是否出现在 PERMISSION-CONTRACT-AUDIT.md 中校验。
    """
    problems: list[Problem] = []
    corpus = "\n".join(_doc(n) for n in CONTRACT_DOCS)
    audit_text = _doc("PERMISSION-CONTRACT-AUDIT.md")
    plan_items = _plan_decisions()
    checked = 0

    for path in _iter_code_files():
        rel = path.relative_to(ROOT).as_posix()
        src = path.read_text(encoding="utf-8")

        for doc_stem, num in _SECTION_REF.findall(src):
            checked += 1
            doc_name = f"{doc_stem}.md"
            if not (DOCS / doc_name).exists():
                problems.append(
                    Problem("doc-ref", f"{rel}:{doc_stem} §{num}", f"{rel}: 引用的 {doc_name} 不存在")
                )
            elif num not in _heading_numbers(_doc(doc_name)):
                problems.append(
                    Problem(
                        "doc-ref",
                        f"{rel}:{doc_stem} §{num}",
                        f"{rel}: {doc_stem} §{num} 不存在",
                    )
                )

        for num in _PLAN_REF.findall(src):
            checked += 1
            if num not in plan_items:
                problems.append(
                    Problem(
                        "doc-ref",
                        f"{rel}:PLAN 决策 #{num}",
                        f"{rel}: PLAN §2 已决事项无第 {num} 条",
                    )
                )

        for token in _NUMBER_REF.findall(src):
            if token in NON_DOC_TOKENS:
                continue
            if token.split("-", 1)[0] in NON_CONTRACT_PREFIXES:
                continue
            checked += 1
            if _AUDIT_TOKEN.fullmatch(token):
                if token not in audit_text:
                    problems.append(
                        Problem(
                            "doc-ref",
                            f"{rel}:{token}",
                            f"{rel}: 审计编号 {token} 在 PERMISSION-CONTRACT-AUDIT.md 中不存在",
                        )
                    )
            elif token not in corpus:
                problems.append(
                    Problem(
                        "doc-ref",
                        f"{rel}:{token}",
                        f"{rel}: 编号 {token} 在 R3 契约文档中不存在",
                    )
                )
    return problems, f"核对 {checked} 处文档引用"


# ---------------------------------------------------------------- 检查 8：契约层符号登记

# 强制逐符号登记 FUNCTION-MAP 的层（业务契约层）。
# app/core 属基础设施层（部署配置、日志、安全原语、响应封装、权限码表），
# 由 DEPLOYMENT / ARCHITECTURE 覆盖，不做逐符号登记。
CONTRACT_LAYERS: tuple[str, ...] = (
    "app/engines/",
    "app/services/",
    "app/tasks/",
    "app/providers/",
    "app/utils/",
)

# 契约层内允许不登记 FUNCTION-MAP 的例外（当前无；新增须在此写明理由）
SYMBOL_DOC_EXEMPT: frozenset[str] = frozenset()


def check_symbols_documented() -> tuple[list[Problem], str]:
    """契约层的公开类/函数必须登记在 FUNCTION-MAP。

    对应流程规则（用户明确要求）：**编码前先更新 FUNCTION-MAP**；若来不及更新，
    代码不得引入未登记的公开符号。本检查把该规则机械化，阻断"先写代码、忘了回填"。
    """
    problems: list[Problem] = []
    fm = _doc("FUNCTION-MAP.md")
    if not fm:
        return [Problem("symbol-doc", "no-doc", "FUNCTION-MAP.md 读取失败")], "文档缺失"

    checked = 0
    for path in _iter_code_files():
        rel = path.relative_to(ROOT).as_posix()
        if not rel.startswith(CONTRACT_LAYERS):
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_") or node.name in SYMBOL_DOC_EXEMPT:
                continue
            checked += 1
            if node.name not in fm:
                problems.append(
                    Problem(
                        "symbol-doc",
                        f"{rel}:{node.name}",
                        f"{rel} 的公开符号 {node.name} 未登记在 FUNCTION-MAP.md；"
                        "按流程规则应先更新 FUNCTION-MAP 再实现",
                    )
                )
    if checked == 0:
        problems.append(
            Problem("symbol-doc", "none-found", "契约层未发现公开符号，检查可能已失效")
        )
    return problems, f"核对 {checked} 个契约层公开符号"


# ---------------------------------------------------------------- 主流程

CHECKS = (
    ("锚点存活自检", check_anchors),
    ("代码 dataclass 均带 slots", check_code_dataclass_slots),
    ("权限码集合与数量一致", check_permission_codes),
    ("文档错误码均有实现", check_error_code_tokens),
    ("HTTP 状态码语义一致", check_http_status),
    ("客户端受限原因码已定义", check_denied_contract),
    ("代码注释的文档引用有效", check_doc_references),
    ("契约层公开符号已登记 FUNCTION-MAP", check_symbols_documented),
)


def _allowance(key: str) -> str | None:
    if key in KNOWN_DIVERGENCES:
        return KNOWN_DIVERGENCES[key]
    return KNOWN_DIVERGENCES.get(f"{key.split(':', 1)[0]}:*")


def main() -> int:
    new_drift: list[Problem] = []
    known: list[tuple[Problem, str]] = []
    triggered: set[str] = set()

    for name, fn in CHECKS:
        raw_problems, note = fn()
        # 同一处引用在文件内重复出现时按 allowlist_key 去重，避免噪声淹没新增漂移
        problems: list[Problem] = []
        seen: set[str] = set()
        for p in raw_problems:
            if p.allowlist_key not in seen:
                seen.add(p.allowlist_key)
                problems.append(p)

        check_drift: list[Problem] = []
        for p in problems:
            allowance = _allowance(p.allowlist_key)
            if allowance is None:
                check_drift.append(p)
                new_drift.append(p)
            else:
                known.append((p, allowance))
                triggered.add(p.allowlist_key)

        print(f"{'[FAIL]' if check_drift else '[ OK ]'} {name}（{note}）")
        for p in check_drift:
            print(f"       ✗ 新增漂移 {p.allowlist_key}：{p.detail}")

    if known:
        print()
        print(f"已知未关闭差异 {len(known)} 处（已在 docs/PERMISSION-CONTRACT-AUDIT.md 登记，不计入退出码）:")
        for p, pa in sorted(known, key=lambda x: (x[1], x[0].allowlist_key)):
            print(f"  [{pa}] {p.allowlist_key}：{p.detail}")

    stale = [k for k in KNOWN_DIVERGENCES if not k.endswith(":*") and k not in triggered]
    if stale:
        print()
        print(f"提示：{len(stale)} 条已知差异已不再触发，应在 KNOWN_DIVERGENCES 中删除：")
        for key in stale:
            print(f"  - {key}")

    print()
    if new_drift:
        print(f"发现 {len(new_drift)} 处新增文档/代码漂移")
        return 1
    print("无新增漂移 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
