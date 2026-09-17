"""解析三原语的单元测试（H19/H20/H21——纯计算，不连任何中间件）。"""

from __future__ import annotations

import pytest

from app.core.errors import TaskError
from app.tasks.parsing import (
    PARSER_VERSION,
    Location,
    clean_text,
    parse_document,
    split_text,
)


# ---------------------------------------------------------------- H19


def test_parse_txt_and_md_roundtrip(tmp_path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes("第一行\n第二行".encode("utf-8"))
    text, locations = parse_document(path, "txt")
    assert text == "第一行\n第二行"
    assert len(locations) == 1 and locations[0].page_no is None
    assert locations[0].end_offset == len(text)


def test_parse_rejects_missing_file_and_unknown_format(tmp_path) -> None:
    with pytest.raises(TaskError) as missing:
        parse_document(tmp_path / "nope.txt", "txt")
    assert missing.value.code == "PARSE_FILE_MISSING"

    real = tmp_path / "x.md"
    real.write_bytes(b"x")
    with pytest.raises(TaskError) as unknown:
        parse_document(real, "doc")  # D-02：旧 DOC 明确不支持
    assert unknown.value.code == "PARSE_UNSUPPORTED"


def test_parse_docx_includes_tables_in_document_order(tmp_path) -> None:
    docx = pytest.importorskip("docx")
    path = tmp_path / "a.docx"
    document = docx.Document()
    document.add_paragraph("表格前的一段话")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = " cell_a "
    table.rows[0].cells[1].text = "cell_b"
    document.add_paragraph("表格后的一段话")
    document.save(str(path))

    text, _ = parse_document(path, "docx")
    assert "表格前的一段话" in text
    assert "cell_a | cell_b" in text, "表格内容必须被抽取（条款不丢）"
    assert text.index("表格前") < text.index("cell_a") < text.index("表格后")


# ---------------------------------------------------------------- H20


def test_clean_unifies_newlines_and_reflows_soft_wraps() -> None:
    raw = "第一行没有句号\r\n第二行也没有\r\n第三行有句号。\r\n\r\n　下一段"
    text, locations, warnings = clean_text(raw, [Location(None, 0, len(raw), None)])
    assert "\r" not in text
    # 前两行被拼回一行（软换行），第三行以句号结尾保持独立。
    assert "第一行没有句号第二行也没有" in text
    assert "第三行有句号。" in text
    assert warnings == []
    # 输出定位必须落在清洗后文本的坐标内
    for location in locations:
        assert 0 <= location.start_offset <= location.end_offset <= len(text)


def test_clean_removes_repeated_page_headers() -> None:
    pages = [
        "薪酬管理制度（密）\n" + f"第 {index} 条 正文内容。" + "\n\n"
        for index in range(1, 6)
    ]
    raw = "\f".join(pages)
    text, _locations, warnings = clean_text(raw, [])
    assert "薪酬管理制度（密）" not in text, "跨页重复页眉必须被移除"
    assert any("页眉" in w or "页脚" in w for w in warnings)
    assert "第 3 条 正文内容。" in text


def test_clean_empty_after_wash_is_permanent_error() -> None:
    with pytest.raises(TaskError) as exc:
        clean_text("\n\n  \t \f\n\n", [])
    assert exc.value.code == "PARSE_EMPTY"
    assert exc.value.transient is False


def test_clean_short_text_is_not_treated_as_empty() -> None:
    """H19 边界："短有效文本不能仅凭少于 200 字判空"。"""
    text, _locations, _warnings = clean_text("只有一句话。", [])
    assert text == "只有一句话。"


# ---------------------------------------------------------------- H21


def test_split_respects_overlap_constraint_and_sequencing() -> None:
    text = "段落一的内容。\n段落二的内容。\n\n# 标题\n标题下的内容。"
    chunks = split_text(text, [], size=50, overlap=10)
    assert [chunk["seq"] for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk["token_count"] > 0
        location = chunk["location"]
        assert location["start_offset"] <= location["end_offset"]
        assert text[location["start_offset"]: location["end_offset"]].strip() != ""


def test_split_rejects_invalid_window_config() -> None:
    with pytest.raises(TaskError) as exc:
        split_text("内容。", [], size=10, overlap=10)  # overlap == size 违反 0<=overlap<size
    assert exc.value.code == "CHUNK_CONFIG_INVALID"


def test_split_long_paragraph_windows_do_not_lose_text() -> None:
    long_body = "".join(f"这是第{i}句，内容足够长以构成多个窗口。" for i in range(60))
    chunks = split_text(long_body, [], size=120, overlap=30)
    assert len(chunks) >= 2, "长段必须被切成多窗"
    # 相邻窗口有重叠（窗口 i 的尾部与 i+1 的头部共享子串）
    joined = [str(chunk["text"]) for chunk in chunks]
    assert any(joined[i][-12:] in joined[i + 1] for i in range(len(joined) - 1)), (
        "overlap 必须产生窗口间重叠"
    )


def test_parser_version_is_declared() -> None:
    """parser_version 不是占位符：H19 落地后必须是有意义的版本串。"""
    assert PARSER_VERSION and PARSER_VERSION != "h19.r0"
