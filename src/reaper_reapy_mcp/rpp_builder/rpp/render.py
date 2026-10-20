"""`RppDocument` -> `.rpp` text, reproducing every byte of the source (F1 writer).

Preference order per line - this is what makes byte-identity structural rather
than a lucky match:

1. `raw` when the reader had to keep the source line (double spaces, tabs
   between tokens, trailing blanks);
2. otherwise the line rebuilt from its tokens and its own indentation.

Line terminators are per line (`eol`), so CRLF stays CRLF and a final line
without a newline does not gain one (RPP_FORMAT.md section 10, item 2). The
writer never formats a number: it writes the token it was given, which is why
`CURSOR 383.31349770757237` keeps all 17 decimals (RPP_FORMAT.md section 8).
"""

from __future__ import annotations

from .node import RppDocument, RppField, RppNode


def render(item: RppDocument | RppNode | RppField) -> str:
    """Serialise a document, a block or a single field back to text."""
    if isinstance(item, RppDocument):
        return "".join(render(child) for child in item.body)
    if isinstance(item, RppNode):
        return _render_node(item)
    if isinstance(item, RppField):
        return (item.raw if item.raw is not None else item.text()) + item.eol
    raise TypeError("cannot render %r" % (type(item).__name__,))


def _render_node(node: RppNode) -> str:
    """Opening line, body in file order, closing line."""
    parts: list[str] = [node.raw_open if node.raw_open is not None else node.open_line()]
    parts.append(node.eol)
    for child in node.body:
        parts.append(render(child))
    parts.append(node.raw_close if node.raw_close is not None else node.close_line())
    parts.append(node.close_eol)
    return "".join(parts)


def render_bytes(item: RppDocument | RppNode | RppField) -> bytes:
    """The exact bytes a file would receive: UTF-8, no BOM (RPP_FORMAT.md section 0)."""
    return render(item).encode("utf-8")
