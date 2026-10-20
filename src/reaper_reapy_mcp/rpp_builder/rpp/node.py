"""Block tree for REAPER `.rpp` text: every token is kept VERBATIM.

Grammar source: `Docs/RPP_FORMAT.md` (REAPER workspace) - section 1 for the
structure, section 8 for the numbers, section 10 for the invariants. Measured
facts this layer is built around:

* A block OPENS on a line whose first non-space character is `<` and CLOSES on
  a line that is exactly `>`. Nesting is a depth counter - indentation carries
  no meaning (measured: 2 spaces per level) and is therefore STORED, never
  recomputed.
* A `<` in the MIDDLE of a token does not open a block: the VST identifier
  `1919247729<56535472656571726561657100000000>` lives inside a single token
  (RPP_FORMAT.md section 9.3).
* Values must survive unchanged - `25.12058` must not become
  `25.120580000000001`, and `CURSOR 383.31349770757237` keeps all 17 decimals
  (RPP_FORMAT.md section 8). So a token is a STRING here; float/int conversion
  belongs to the model layer (F2), never to this one.

Byte-identity is STRUCTURAL, not hopeful: a line is rebuilt from
`indent + " ".join(tokens)` only when that equals the source line; otherwise
the source line is kept in `raw`. Double spaces, tabs between tokens and
trailing blanks therefore round-trip without a special case each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Union

# REAPER on Windows writes CRLF on every line (RPP_FORMAT.md section 10, item 2),
# so that is the default a freshly built line inherits.
CRLF = "\r\n"


class RppParseError(ValueError):
    """The text cannot be a block tree: a stray `>` or an unclosed `<`."""


@dataclass(slots=True)
class RppField:
    """One non-block line, e.g. `NAME "Ins - ins.mid"`.

    Attributes:
        tokens: the line's tokens verbatim, quotes included.
        indent: leading whitespace, exactly as found.
        eol: that line's terminator ("\\r\\n", "\\n", or "" for a last line
            without one).
        raw: the exact line content when `indent` plus the joined tokens cannot
            reproduce it (indentation included; the terminator excluded).
    """

    tokens: tuple[str, ...] = ()
    indent: str = ""
    eol: str = CRLF
    raw: str | None = None

    @property
    def key(self) -> str:
        """The first token, which is the token name (`NAME`, `POSITION`, `E`)."""
        return self.tokens[0] if self.tokens else ""

    @property
    def args(self) -> tuple[str, ...]:
        """Everything after the token name, quotes verbatim."""
        return self.tokens[1:]

    @property
    def value(self) -> str:
        """The single argument of a one-argument field, else the empty string.

        Quotes are NOT removed: `NAME "Ins - ins.mid"` yields `"Ins - ins.mid"`.
        Use `unquote()` when the text is needed without them.
        """
        return self.tokens[1] if len(self.tokens) > 1 else ""

    def text(self) -> str:
        """The line content, indentation included, without the terminator."""
        if self.raw is not None:
            return self.raw
        if not self.tokens:
            return self.indent
        return self.indent + " ".join(self.tokens)


@dataclass(slots=True)
class RppNode:
    """A `<TAG args ...>` block with an ordered body.

    Attributes:
        tag: the tag name without `<` (`TRACK`, `ITEM`, `SOURCE`, `VST`, `X`).
        args: the remaining tokens of the opening line, verbatim.
        body: children in file order; fields and nested blocks interleaved.
        indent: leading whitespace of the opening line.
        eol: terminator of the opening line.
        close_indent: leading whitespace of the closing `>` line.
        close_eol: terminator of the closing line.
        raw_open / raw_close: exact source lines when the rebuilt ones differ.
    """

    tag: str
    args: tuple[str, ...] = ()
    body: list[Union["RppNode", RppField]] = field(default_factory=list)
    indent: str = ""
    eol: str = CRLF
    close_indent: str = ""
    close_eol: str = CRLF
    raw_open: str | None = None
    raw_close: str | None = None

    # -- construction helpers -------------------------------------------------

    def open_line(self) -> str:
        """The opening line rebuilt from tag and args (indent included)."""
        if not self.args:
            return "%s<%s" % (self.indent, self.tag)
        return "%s<%s %s" % (self.indent, self.tag, " ".join(self.args))

    def close_line(self) -> str:
        """The closing line rebuilt from its own indentation."""
        return self.close_indent + ">"

    # -- navigation (DEPTH-AWARE on purpose) ---------------------------------

    def fields(self) -> Iterator[RppField]:
        """Direct child fields only - never the fields of a nested block.

        This is the whole point of the layer: a `NAME` inside `<ITEM>` belongs
        to the item, not to its track (RPP_FORMAT.md section 1, trap 1).
        """
        for child in self.body:
            if isinstance(child, RppField):
                yield child

    def nodes(self) -> Iterator["RppNode"]:
        """Direct child blocks only."""
        for child in self.body:
            if isinstance(child, RppNode):
                yield child

    def find(self, tag: str) -> "RppNode | None":
        """First direct child block with that tag, or None."""
        for child in self.nodes():
            if child.tag == tag:
                return child
        return None

    def find_all(self, tag: str) -> list["RppNode"]:
        """All direct child blocks with that tag, in file order."""
        return [child for child in self.nodes() if child.tag == tag]

    def field(self, key: str) -> RppField | None:
        """First direct child field with that token name, or None."""
        for child in self.fields():
            if child.key == key:
                return child
        return None

    def field_value(self, key: str) -> str:
        """`field(key).value`, or the empty string when the field is absent."""
        found = self.field(key)
        return found.value if found is not None else ""

    def walk(self) -> Iterator["RppNode"]:
        """This block and every nested block, in file order (pre-order)."""
        yield self
        for child in self.nodes():
            yield from child.walk()


@dataclass(slots=True)
class RppDocument:
    """A whole `.rpp` file: normally one `<REAPER_PROJECT>` block.

    `body` is a list because the format permits lines before the root block in
    principle; the reference files have exactly one root and nothing else.
    """

    body: list[Union[RppNode, RppField]] = field(default_factory=list)

    @property
    def root(self) -> RppNode | None:
        """The first top-level block (the project block in a real `.rpp`)."""
        for item in self.body:
            if isinstance(item, RppNode):
                return item
        return None

    def walk(self) -> Iterator[RppNode]:
        """Every block of the document, in file order (pre-order)."""
        for item in self.body:
            if isinstance(item, RppNode):
                yield from item.walk()


def unquote(token: str) -> str:
    """Strip the surrounding double quotes of a token, if it has both.

    Quote characters are part of a token by design; this helper is for callers
    that need the text itself (e.g. a track name) and never for rendering.
    """
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    return token
