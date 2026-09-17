"""文档解析三原语（H19/H20/H21，FUNCTION-MAP §4）。

三个函数都是**纯计算**（契约："格式解析器/token计数器，不调用LLM"），因此可以
不依赖任何中间件地完整测试；它们只负责"文件 → 干净文本 → 带定位的切片"。

三条刻意的取舍（都能在注释里找到理由）：

1. **清洗后的偏移量为准**：`Location` 的 `start/end` 指**清洗后文本**的码点偏移
   （切片与引用都发生在清洗后的文本上，这样偏移才可复算）；清洗会重排文本，
   原文偏移无法可靠映射时 `original_offset=None`——**不伪造精确位置**
   （FORMAT-ACCEPTANCE §1）。
2. **页分隔用 `\f`**：PDF/DOCX 的"页"是解析器概念；H20 以 `\f` 分页做页眉脚检测，
   输出时再折叠掉。MD/TXT 没有"页"，`page_no=None`。
3. **token 数是启发式估计**：契约要求"字符不当token"。本地不引入分词器
   （多一份重依赖且各模型分词不同），用 CJK≈1 token/字、其他≈4 字符/token 的
   经验估计做切片窗口；真实 token 数由 H11 调用返回的 usage 记录——估计值只用于
   **切窗**，不用于计费口径。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import TaskError

#: 解析器版本（写入 knowledge_version.parser_version）。算法/清洗规则变更时必须递增，
#: 否则"同 file_key 在新解析器下结果不同"无法被发现。
PARSER_VERSION = "h19.r1"

#: 页分隔符：PDF/DOCX 逐页文本用它拼接，H20 据此分页做页眉脚检测。
PAGE_SEP = "\f"

#: 切窗用的 CJK 区段（常用汉字 + CJK 标点 + 全角符号）。
_CJK_RANGES = (("\u4e00", "\u9fff"), ("\u3000", "\u303f"), ("\uff00", "\uffef"))

#: 句末标点：一行以此结尾时，重流（reflow）不把下一行拼上来。
_SENTENCE_ENDINGS = "。！？；：…\"”』」）】!?;:"


@dataclass(frozen=True, slots=True)
class Location:
    """切片定位（FUNCTION-MAP §1：page_no/start_offset/end_offset/original_offset）。"""

    page_no: int | None
    start_offset: int
    end_offset: int
    original_offset: int | None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "page_no": self.page_no,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "original_offset": self.original_offset,
        }


def estimate_tokens(text: str) -> int:
    """启发式 token 估计：CJK≈1 token/字，其余≈4 字符/token。

    只用于切窗（H21）与切片行 `token_count` 的落库口径；真实用量由 H11 的
    响应 usage 记录——两者口径不同，不要互相替代。
    （2026-09-17 由私有转公开：F-04.05 切片编辑在服务层预建新版本切片行时
    需要同一口径，避免两处各写一个估计器造成漂移。）
    """
    cjk = sum(
        1
        for ch in text
        if any(lo <= ch <= hi for lo, hi in _CJK_RANGES)
    )
    return cjk + math.ceil(max(0, len(text) - cjk) / 4)


def _decode(raw: bytes) -> str:
    """按 UTF-8 → GBK 顺序解码；都不行就是文件损坏（不静默用替代字符吞掉）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise TaskError("PARSE_CORRUPT", "文本不是合法的 UTF-8/GBK 编码", transient=False)


# ---------------------------------------------------------------- H19


def parse_document(path: Path, format: str) -> tuple[str, list[Location]]:
    """H19：按 pdf/docx/md/txt 提取文本与页段定位。

    返回的 text 是**原始抽取文本**（PDF/DOCX 的页之间用 `\f` 分隔），
    尚未清洗——清洗是 H20 的职责，两层分开才能分别测试。
    """
    if not path.is_file():
        raise TaskError("PARSE_FILE_MISSING", f"文件不存在：{path}", transient=False)

    fmt = format.lower().lstrip(".")
    try:
        if fmt in ("md", "txt"):
            text = _decode(path.read_bytes())
            return text, [Location(None, 0, len(text), None)]
        if fmt == "pdf":
            return _parse_pdf(path)
        if fmt == "docx":
            return _parse_docx(path)
    except TaskError:
        raise
    except Exception as exc:  # 解析器抛出的任何其他异常都归为"损坏"
        raise TaskError("PARSE_CORRUPT", f"{fmt} 解析失败：{exc}", transient=False) from exc

    raise TaskError("PARSE_UNSUPPORTED", f"不支持的解析格式：{fmt}", transient=False)


def _parse_pdf(path: Path) -> tuple[str, list[Location]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise TaskError("PARSE_ENCRYPTED", "PDF 已加密，拒绝解析", transient=False)

    pages: list[str] = []
    locations: list[Location] = []
    offset = 0
    for index, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        pages.append(page_text)
        locations.append(Location(index, offset, offset + len(page_text), None))
        offset += len(page_text) + 1  # +1 为页分隔符
    return PAGE_SEP.join(pages), locations


def _parse_docx(path: Path) -> tuple[str, list[Location]]:
    """DOCX 没有"页"的概念；正文与表格按**文档顺序**抽取（表格是正文的一部分，
    "不删表格数字条款"从解析层就开始成立，而不是留给清洗层）。"""
    import docx

    document = docx.Document(str(path))
    parts: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            from docx.text.paragraph import Paragraph

            parts.append(Paragraph(child, document).text)
        elif tag == "tbl":
            from docx.table import Table

            table = Table(child, document)
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))

    text = "\n".join(parts)
    return text, [Location(None, 0, len(text), None)]


# ---------------------------------------------------------------- H20


def clean_text(
    text: str, locations: list[Location]
) -> tuple[str, list[Location], list[str]]:
    """H20：Unicode 规范化、换行统一、控制符清理、页眉脚检测、保守重流、空文本校验。"""
    warnings: list[str] = []

    # 1) 规范化与换行统一（\r\n/\r → \n）
    cleaned = unicodedata.normalize("NFC", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    # 2) 控制符清理：保留 \n（换行）、\f（页分隔，稍后折叠）、\t；其余 C0 控制符删除。
    cleaned = "".join(
        ch for ch in cleaned if ch in ("\n", "\t", "\f") or not unicodedata.category(ch) == "Cc"
    )

    # 3) 分页，逐页做行级清理
    raw_pages = cleaned.split(PAGE_SEP)
    had_pages = len(raw_pages) > 1
    page_lines = [[line.rstrip() for line in page.split("\n")] for page in raw_pages]

    # 4) 跨页重复页眉/页脚：仅在页数足够时启用（页太少时"重复"可能是正文本身）。
    if had_pages and len(page_lines) >= 3:
        suspects: dict[str, int] = {}
        for lines in page_lines:
            non_empty = [line for line in lines if line.strip()]
            for line in {non_empty[0], non_empty[-1]} if non_empty else ():
                suspects[line] = suspects.get(line, 0) + 1
        threshold = math.ceil(len(page_lines) * 0.6)
        repeated = {line for line, count in suspects.items() if count >= threshold and line.strip()}
        if repeated:
            page_lines = [
                [
                    line
                    for index, line in enumerate(lines)
                    if not (line in repeated and _is_edge_line(lines, index))
                ]
                for lines in page_lines
            ]
            warnings.append(f"已移除跨页重复页眉/页脚 {len(repeated)} 条")

    # 5) 保守重流：同一段落内的软换行拼回一行。
    #    规则：上一行**不以句末标点结尾**且下一行不是标题/列表/表格行时才拼接；
    #    拼不上的情况保持原样——这是"保守"的含义，宁可多留换行也不吞掉结构。
    reflowed_pages: list[list[str]] = []
    for lines in page_lines:
        merged: list[str] = []
        for line in lines:
            stripped = line.strip()
            if (
                merged
                and merged[-1]
                and not merged[-1].rstrip().endswith(tuple(_SENTENCE_ENDINGS))
                # 下一行不是标题/列表/表格/引用行（否则吞掉结构）……
                and not stripped.startswith(("#", "-", "*", "|", ">", "1.", "(", "（"))
                # ……且**上一行也不是标题**：标题永远独立成段，
                # 拼进正文会让"# 薪酬制度"变成"# 薪酬制度第1条……"（实测踩过）。
                and not merged[-1].startswith("#")
            ):
                merged[-1] = merged[-1].rstrip() + stripped
            else:
                merged.append(stripped)
        reflowed_pages.append(merged)

    # 6) 过多空行折叠为单空行，并重组文本（页之间用空行分隔，\f 不再保留）。
    out_pages: list[str] = []
    for lines in reflowed_pages:
        block: list[str] = []
        blank = False
        for line in lines:
            if line:
                block.append(line)
                blank = False
            elif block and not blank:
                block.append("")
                blank = True
        out_pages.append("\n".join(block).strip("\n"))
    final_text = "\n\n".join(page for page in out_pages if page.strip())

    # 7) 空文本校验：**在清洗之后**判——"短有效文本不能仅凭少于 200 字判空"（H19 边界）。
    if not final_text.strip():
        raise TaskError("PARSE_EMPTY", "清洗后无有效文本", transient=False)

    # 8) 重建页定位：输出偏移以**清洗后文本**为准；原文偏移无法可靠映射 → None。
    rebuilt: list[Location] = []
    cursor = 0
    page_no = 0
    for page in out_pages:
        if not page.strip():
            continue
        page_no += 1 if had_pages else 0
        start = final_text.find(page, cursor)
        end = start + len(page)
        rebuilt.append(Location(page_no or None, start, end, None))
        cursor = end
    if not rebuilt:  # 单页（MD/TXT）兜底
        rebuilt = [Location(None, 0, len(final_text), None)]
    _ = locations  # 输入定位只用于确认页序；清洗后重建，原偏移不保留（见模块注释）
    return final_text, rebuilt, warnings


def _is_edge_line(lines: list[str], index: int) -> bool:
    """该行是否处于"页首/页尾"位置（页眉脚只出现在边缘）。"""
    non_empty_indexes = [i for i, line in enumerate(lines) if line.strip()]
    if not non_empty_indexes:
        return False
    return index in (non_empty_indexes[0], non_empty_indexes[-1])


# ---------------------------------------------------------------- H21


def split_text(
    text: str, locations: list[Location], size: int, overlap: int
) -> list[dict[str, object]]:
    """H21：标题/段落优先切片，长段按 token 窗口滑动。

    返回 `[{seq, text, token_count, location}]`；`0 <= overlap < size`。
    """
    if not 0 <= overlap < size:
        raise TaskError("CHUNK_CONFIG_INVALID", f"要求 0 <= overlap < size，当前 {overlap}/{size}", transient=False)

    segments = _segments(text)
    chunks: list[dict[str, object]] = []
    seq = 0
    for start, end in segments:
        segment = text[start:end]
        if estimate_tokens(segment) <= size:
            chunks.append(_make_chunk(seq, segment, start, end, locations))
            seq += 1
            continue
        # 长段：按子单元（句子）贪心装窗，窗口 ≤ size token，重叠 ≥ overlap token。
        units = [
            (m.start() + start, m.end() + start)
            for m in _unit_iter(segment)
            if m.end() > m.start()
        ]
        window_start = 0
        while window_start < len(units):
            window: list[tuple[int, int]] = []
            tokens = 0
            cursor = window_start
            while cursor < len(units) and tokens < size:
                unit_text = text[units[cursor][0]: units[cursor][1]]
                if window and tokens + estimate_tokens(unit_text) > size:
                    break
                window.append(units[cursor])
                tokens += estimate_tokens(unit_text)
                cursor += 1
            if not window:  # 单个子单元就超长：按字符硬切（不静默截断语义，见下）
                span_start, span_end = units[window_start]
                window = [(span_start, min(span_end, span_start + size * 4))]
                cursor = window_start + 1
            first, last = window[0][0], window[-1][1]
            chunks.append(_make_chunk(seq, text[first:last], first, last, locations))
            seq += 1
            if cursor >= len(units):
                break
            # 重叠：从窗口尾部回退若干子单元，使重叠估计 ≥ overlap token。
            back_tokens = 0
            back = 0
            while back < len(window) - 1 and back_tokens < overlap:
                back += 1
                back_tokens += estimate_tokens(
                    text[window[-back][0]: window[-back][1]]
                )
            window_start = cursor - back
    return chunks


def _make_chunk(
    seq: int, chunk_text: str, start: int, end: int, locations: list[Location]
) -> dict[str, object]:
    """为切片定位：找到包含 start 的页；找不到（理论不可达）按无页处理。"""
    page_no = next(
        (loc.page_no for loc in locations if loc.start_offset <= start < loc.end_offset),
        None,
    )
    return {
        "seq": seq,
        "text": chunk_text,
        "token_count": estimate_tokens(chunk_text),
        "location": Location(page_no, start, end, None).to_dict(),
    }


def _segments(text: str) -> list[tuple[int, int]]:
    """把文本切成顶层片段：markdown 标题单独起段，其余按空行分段。"""
    marks = [0]
    for line_match in _line_starts(text):
        if text[line_match:].startswith(("#",)) and (line_match == 0 or text[line_match - 1] == "\n"):
            if line_match != marks[-1]:
                marks.append(line_match)
    marks.append(len(text))
    spans = [(marks[i], marks[i + 1]) for i in range(len(marks) - 1)]

    result: list[tuple[int, int]] = []
    for span_start, span_end in spans:
        blank = None
        piece_start = span_start
        for offset in range(span_start, span_end):
            if text[offset] == "\n":
                if offset + 1 < span_end and text[offset + 1] == "\n":
                    if piece_start < offset + 1:
                        result.append((piece_start, offset + 1))
                    piece_start = blank = offset + 2
                    _ = blank
        if piece_start < span_end:
            result.append((piece_start, span_end))
    return [(a, b) for a, b in result if text[a:b].strip()]


def _line_starts(text: str) -> list[int]:
    return [m.start() for m in re.finditer(r"(?m)^", text)]


#: 子单元 = 句子（到句末标点为止，标点随前句）。★ 不能按"行"切：
#: 连续长段可能整段没有换行，按行切会得到"一个单元 = 整段"，窗口永远装不下，
#: 于是长段被整体当成一个切片返回——这是本轮实测抓到的缺陷。
_SENTENCE_RE = re.compile(r"[^。！？；!?;\n]*[。！？；!?;\n]?")


def _unit_iter(segment: str):
    return _SENTENCE_RE.finditer(segment)
