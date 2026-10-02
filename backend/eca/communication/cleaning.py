"""Deterministic body cleaning: HTML to text, quoted replies and signatures (slice 1.3).

Forwarded blocks are kept (statements inside them are attributed to the forwarded author,
AI_PIPELINE.md §5.5); quoted reply history is removed so a restated promise in quoted text does
not create evidence twice (CC-40).
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

FORWARD_MARKERS = (
    "---------- Forwarded message ---------",
    "-----Forwarded Message-----",
    "Begin forwarded message:",
)
_QUOTE_HEADER = re.compile(r"^On .{3,200}wrote:\s*$")
_OUTLOOK_SEPARATOR = re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE)
_MOBILE_SIG = re.compile(r"^Sent from my \w+", re.IGNORECASE)
_BLANKS = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    _BLOCK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return html.unescape("".join(parser.parts))


def clean_body(body_text: str | None, body_html: str | None = None) -> str:
    """The text sent to extraction: no quoted history, no signature, forwarded blocks kept."""
    text = body_text if body_text else (html_to_text(body_html) if body_html else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    in_forward = False
    for line in text.split("\n"):
        stripped = line.strip()
        if any(stripped.startswith(m) for m in FORWARD_MARKERS):
            in_forward = True
            out.append(line)
            continue
        if not in_forward:
            if _QUOTE_HEADER.match(stripped) or _OUTLOOK_SEPARATOR.match(stripped):
                break
            if stripped.startswith(">"):
                continue
            if line.rstrip("\n") == "-- " or stripped == "--" or _MOBILE_SIG.match(stripped):
                break
        out.append(line)
    return _BLANKS.sub("\n\n", "\n".join(out)).strip()


def split_forwarded(body_clean: str) -> tuple[str, str | None]:
    """(own text, forwarded block or None)."""
    for marker in FORWARD_MARKERS:
        idx = body_clean.find(marker)
        if idx >= 0:
            return body_clean[:idx].rstrip(), body_clean[idx:]
    return body_clean, None


def snippet(body_clean: str, limit: int = 160) -> str:
    flat = " ".join(body_clean.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
