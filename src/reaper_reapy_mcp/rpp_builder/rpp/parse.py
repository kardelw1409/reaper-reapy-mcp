"""`.rpp` text -> `RppDocument` (F1 reader).

Two rules are enough to parse any `.rpp` (RPP_FORMAT.md section 1):

1. a line whose first non-space character is `<` OPENS a block;
2. a line that is exactly `>` CLOSES the innermost open block.

Structure is therefore a depth counter. Indentation is decoration and is kept
per line, never used to decide nesting - a depth-blind scan of `NAME` silently
returns item names for tracks, and the result looks plausible (14 tracks named
`song.wav`, `vocals.wav`, ... instead of `AI_MASTER`, `AI_VOCAL`, ...).

The reader never interprets a value: `25.12058`, `1919247729<565354...>`,
`"Ins - ins.mid"` and `''` stay exactly as written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .node import RppDocument, RppField, RppNode, RppParseError

_UTF8_BOM = b"\xef\xbb\xbf"


def tokenize(text: str) -> tuple[str, ...]:
    """Split one line into tokens, keeping quotes and never splitting inside them.

    The token keeps its surrounding double quotes (`NAME "Ins - ins.mid"` gives
    `('NAME', '"Ins - ins.mid"')`), because a writer that re-quoted a value
    would have to decide how to escape - and REAPER has no escape sequence
    measured (RPP_FORMAT.md section 8).

    A `<` or `>` in the middle of a token is an ordinary character: the VST
    identifier token `1919247729<56535472656571726561657100000000>` stays whole
    (RPP_FORMAT.md section 9.3).

    An unterminated quote consumes the rest of the line, which lets a broken
    line round-trip through `RppField.raw` instead of raising here.
    """
    tokens: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        start = index
        if char == '"':
            index += 1
            while index < length and text[index] != '"':
                index += 1
            if index < length:
                index += 1  # include the closing quote
        else:
            while index < length and not text[index].isspace():
                index += 1
        tokens.append(text[start:index])
    return tuple(tokens)


def iter_lines(text: str) -> Iterator[tuple[str, str]]:
    """Yield `(line without terminator, terminator as found)`.

    The terminator is preserved per line on purpose: CRLF must come back as
    CRLF (RPP_FORMAT.md section 10, item 2), and a last line without a newline
    must not gain one. `str.splitlines()` would lose both facts.
    """
    start = 0
    length = len(text)
    while start < length:
        newline_at = text.find("\n", start)
        if newline_at < 0:
            yield text[start:], ""
            return
        line = text[start:newline_at]
        if line.endswith("\r"):
            yield line[:-1], "\r\n"
        else:
            yield line, "\n"
        start = newline_at + 1


def parse(text: str) -> RppDocument:
    """Parse a whole `.rpp` text into a lossless tree.

    Raises:
        RppParseError: a `>` with nothing open, or a block left unclosed at the
            end of the file (both make the depth counter non-zero, and a
            non-zero balance means every later block would be misplaced).
    """
    document = RppDocument()
    stack: list[RppNode] = []
    opened_at: list[int] = []

    for number, (line, eol) in enumerate(iter_lines(text), start=1):
        target = stack[-1].body if stack else document.body

        if not line.strip():
            # A blank (or whitespace-only) line: keep it verbatim so the count of
            # lines and their bytes do not change.
            target.append(RppField(tokens=(), indent="", eol=eol, raw=line))
            continue

        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        if stripped.rstrip() == ">":
            if not stack:
                raise RppParseError(
                    "line %d: closing '>' with no open block" % number
                )
            node = stack.pop()
            opened_at.pop()
            node.close_indent = indent
            node.close_eol = eol
            if node.close_line() != line:
                node.raw_close = line
            continue

        if stripped.startswith("<"):
            inner = tokenize(stripped[1:])
            if not inner:
                raise RppParseError("line %d: '<' with no tag name" % number)
            node = RppNode(
                tag=inner[0],
                args=inner[1:],
                indent=indent,
                eol=eol,
                close_indent=indent,
            )
            if node.open_line() != line:
                node.raw_open = line
            target.append(node)
            stack.append(node)
            opened_at.append(number)
            continue

        field_node = RppField(tokens=tokenize(stripped), indent=indent, eol=eol)
        if field_node.text() != line:
            # Double spaces, a tab between tokens, trailing blanks: the line is
            # not reproducible from its tokens, so the source text is kept.
            field_node.raw = line
        target.append(field_node)

    if stack:
        raise RppParseError(
            "line %d: block <%s> is never closed (depth %d at end of file)"
            % (opened_at[-1], stack[-1].tag, len(stack))
        )
    return document


def parse_file(path: str | Path) -> RppDocument:
    """Read and parse a `.rpp` file as bytes, so CRLF is not translated away.

    A UTF-8 BOM is refused rather than silently dropped: REAPER writes UTF-8
    without one (RPP_FORMAT.md section 0), and dropping it would break the
    byte-identical write that F1 promises.
    """
    data = Path(path).read_bytes()
    if data.startswith(_UTF8_BOM):
        raise RppParseError(
            "%s starts with a UTF-8 BOM; REAPER writes UTF-8 without one, and a "
            "lossless round-trip cannot keep a BOM this layer does not model" % path
        )
    return parse(data.decode("utf-8"))
