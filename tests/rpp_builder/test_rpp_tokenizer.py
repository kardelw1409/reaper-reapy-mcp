"""Tokenizer and tree-shape unit tests (F1).

Each test quotes the measured fact from `Docs/RPP_FORMAT.md` it exercises, so a
failure means the format understanding changed, not that a style choice did.
"""

from __future__ import annotations

import pytest

from reaper_reapy_mcp.rpp_builder import (
    RppDocument,
    RppField,
    RppNode,
    RppParseError,
    parse,
    render,
    tokenize,
)


# --- tokenizer --------------------------------------------------------------


def test_quoted_value_is_one_token_and_quotes_are_kept():
    # RPP_FORMAT.md section 8: a value containing spaces is quoted.
    assert tokenize('NAME "Ins - ins.mid"') == ("NAME", '"Ins - ins.mid"')


def test_empty_quoted_value_is_a_token():
    # RPP_FORMAT.md section 9.5: an empty name is written as "".
    assert tokenize('NAME ""') == ("NAME", '""')


def test_angle_brackets_inside_a_token_do_not_split_it():
    # RPP_FORMAT.md section 9.3: the plugin id is `1919247729<565354...>`.
    line = (
        '"VST: ReaEQ (Cockos)" reaeq.dll 0 "" '
        "1919247729<56535472656571726561657100000000> \"\""
    )
    tokens = tokenize(line)
    assert tokens[4] == "1919247729<56535472656571726561657100000000>"
    assert " ".join(tokens) == line


def test_single_quotes_are_ordinary_characters():
    # RPP_FORMAT.md section 9.2: AUXRECV ends with ''.
    line = "AUXRECV 1 0 0.5 0 0 0 0 0 0 -1:U 0 -1 ''"
    assert tokenize(line)[-1] == "''"
    assert len(tokenize(line)) == 14


def test_unterminated_quote_consumes_the_rest_of_the_line():
    # No escape sequence is measured (RPP_FORMAT.md section 8), so a stray quote
    # must not raise here: the line round-trips through `raw` instead.
    assert tokenize('NAME "unclosed value') == ("NAME", '"unclosed value')


# --- structure --------------------------------------------------------------


def test_depth_not_indentation_decides_nesting():
    text = "<X\r\n    NAME outer\r\n  <Y\r\n        NAME inner\r\n  >\r\n>\r\n"
    root = parse(text).root
    assert isinstance(root, RppNode)
    assert root.field_value("NAME") == "outer"
    inner = root.find("Y")
    assert inner is not None
    assert inner.field_value("NAME") == "inner"
    # Indentation is stored, not recomputed: 4 spaces belong to `NAME outer`.
    assert root.field("NAME").indent == "    "
    assert inner.indent == "  "
    assert render(parse(text)) == text


def test_lt_in_the_middle_of_a_line_is_not_a_block():
    text = "<X\r\n  HASDATA 1<480> QN\r\n>\r\n"
    root = parse(text).root
    assert [child.tag for child in root.nodes()] == []
    assert root.field("HASDATA").args == ("1<480>", "QN")
    assert render(parse(text)) == text


def test_double_space_survives_through_raw():
    text = "<X\r\n  NAME a  b\r\n>\r\n"
    field = parse(text).root.field("NAME")
    assert field.tokens == ("NAME", "a", "b")
    assert field.raw == "  NAME a  b"
    assert render(parse(text)) == text


def test_tab_between_tokens_survives_through_raw():
    text = "<X\r\n  VOLPAN 1\t0\r\n>\r\n"
    field = parse(text).root.field("VOLPAN")
    assert field.tokens == ("VOLPAN", "1", "0")
    assert field.raw == "  VOLPAN 1\t0"
    assert render(parse(text)) == text


def test_blank_line_is_preserved():
    text = "<X\r\n\r\n  NAME a\r\n>\r\n"
    root = parse(text).root
    assert isinstance(root.body[0], RppField)
    assert root.body[0].tokens == ()
    assert render(parse(text)) == text


def test_final_line_without_newline_does_not_gain_one():
    text = "<X\r\n  NAME a\r\n>"
    assert render(parse(text)) == text


def test_stray_closer_raises_with_line_number():
    with pytest.raises(RppParseError) as caught:
        parse("<X\r\n>\r\n>\r\n")
    assert "line 3" in str(caught.value)


def test_unclosed_block_raises_with_opening_line():
    with pytest.raises(RppParseError) as caught:
        parse("<X\r\n  <Y\r\n  >\r\n")
    assert "line 1" in str(caught.value)
    assert "X" in str(caught.value)


def test_empty_text_is_an_empty_document():
    document = parse("")
    assert isinstance(document, RppDocument)
    assert document.body == []
    assert render(document) == ""


def test_node_navigation_is_depth_aware():
    text = "<TRACK {A}\r\n  NAME T1\r\n  <ITEM\r\n    NAME take.wav\r\n  >\r\n>\r\n"
    track = parse(text).root
    assert track.tag == "TRACK"
    assert track.args == ("{A}",)
    assert track.field_value("NAME") == "T1"
    assert track.find("ITEM").field_value("NAME") == "take.wav"
    assert [node.tag for node in track.walk()] == ["TRACK", "ITEM"]
