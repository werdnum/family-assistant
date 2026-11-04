"""Separate the text a sender typed from what their mail client quoted.

Gmail marks the material it adds below a reply or forward: the quoted thread
and a forwarded message both sit in a ``div.gmail_quote``, and a saved
signature in a ``div.gmail_signature``. Removing those leaves what the sender
wrote. A message with no HTML part falls back to the plain-text equivalents
(the ``On ... wrote:`` attribution, ``>`` quoting, the forwarded-message
banner and the ``-- `` signature separator).

This is a convenience for the household's own mail, not a parser for hostile
input: the only caller applies it to mail that passed DMARC from an address
mapped to a household member.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from markdownify import markdownify

_REMOVED_CLASSES = frozenset({
    "gmail_quote",
    "gmail_quote_container",
    "gmail_signature",
    "gmail_signature_prefix",
})
_VOID_ELEMENTS = frozenset({
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
})
_DROPPED_ELEMENTS = frozenset({"blockquote", "head", "script", "style", "title"})

_PLAIN_CUT_RE = re.compile(
    r"""
    ^-{5,}\s*Forwarded\ message\s*-{5,}\s*$      # Gmail forward banner
    | ^On\s[^\n]*(?:\n[^\n]*)?\swrote:\s*$       # reply attribution, maybe wrapped
    | ^>                                        # quoted line
    | ^--\ $                                    # signature separator
    """,
    re.MULTILINE | re.VERBOSE,
)


class _AuthoredHtmlFilter(HTMLParser):
    """Re-emit HTML with Gmail's quote and signature blocks removed."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.kept: list[str] = []
        self._skip_depth = 0

    def _starts_removed_block(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> bool:
        if tag in _DROPPED_ELEMENTS:
            return True
        classes = next((value for name, value in attrs if name == "class"), None) or ""
        return any(name in _REMOVED_CLASSES for name in classes.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_ELEMENTS:
            if self._skip_depth == 0:
                self.kept.append(self.get_starttag_text() or "")
            return
        if self._skip_depth > 0:
            self._skip_depth += 1
            return
        if self._starts_removed_block(tag, attrs):
            self._skip_depth = 1
            return
        self.kept.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth == 0:
            self.kept.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_ELEMENTS:
            return
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        self.kept.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.kept.append(html.escape(data, quote=False))


def _authored_from_html(body_html: str) -> str:
    parser = _AuthoredHtmlFilter()
    parser.feed(body_html)
    parser.close()
    text = markdownify("".join(parser.kept), heading_style="ATX")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _authored_from_plain(body_plain: str) -> str:
    text = body_plain.replace("\r\n", "\n")
    match = _PLAIN_CUT_RE.search(text)
    if match is not None:
        text = text[: match.start()]
    return text.strip()


def extract_authored_text(*, body_html: str | None, body_plain: str | None) -> str:
    """Return the text the sender wrote, without quoted or forwarded material.

    Empty when the sender added nothing of their own, as in a bare forward.
    """
    if body_html and body_html.strip():
        return _authored_from_html(body_html)
    if body_plain:
        return _authored_from_plain(body_plain)
    return ""
