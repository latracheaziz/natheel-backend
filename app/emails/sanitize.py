"""Allow-list sanitiser for HTML email bodies.

Email HTML is attacker-controlled: anyone can send a message. It is cleaned here, on the server,
before it ever reaches the admin page, and the page additionally renders it inside a sandboxed
iframe, so a miss in either layer is not enough on its own to run code.

Dropped outright: <script>, <style>, <iframe>, <object>, <embed>, <form>, <link>, <meta>, <base>
and everything inside them. Dropped per attribute: every `on*` handler, anything whose URL scheme
is not http/https/mailto/cid, and `srcdoc`/`formaction`-style sinks. Only stdlib is used — no new
dependency for a security-critical path whose behaviour we want to be able to read in full.
"""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser

# Structural and formatting tags that carry an email's meaning.
ALLOWED_TAGS = frozenset({
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "code", "col", "colgroup", "dd",
    "div", "dl", "dt", "em", "figcaption", "figure", "font", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "img", "li", "ol", "p", "pre", "q", "s", "small", "span", "strike", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul", "wbr",
})

# Tags whose *contents* must go too, not just the tag.
DROP_WITH_CONTENT = frozenset({"script", "style", "iframe", "object", "embed", "form", "svg",
                               "math", "template", "noscript", "title", "head"})

VOID_TAGS = frozenset({"br", "hr", "img", "col", "wbr"})

ALLOWED_ATTRS = {
    "a": frozenset({"href", "title", "target", "rel"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "align", "valign"}),
    "th": frozenset({"colspan", "rowspan", "align", "valign"}),
    "table": frozenset({"border", "cellpadding", "cellspacing", "width", "align"}),
    "col": frozenset({"span", "width"}),
    "colgroup": frozenset({"span", "width"}),
    "font": frozenset({"color", "face", "size"}),
}
# Allowed on any permitted tag. `style` is kept because email layout depends on it, and the
# sandboxed iframe plus the url filter below keep it from reaching anything executable.
GLOBAL_ATTRS = frozenset({"style", "align", "dir", "lang"})

URL_ATTRS = frozenset({"href", "src"})
SAFE_SCHEMES = ("http://", "https://", "mailto:", "cid:", "data:image/")
# CSS constructs that can fetch or execute; `url(` is dropped so styles cannot beacon out either.
CSS_BLOCKED = ("javascript:", "expression(", "url(", "@import", "behavior:", "-moz-binding")


def _safe_url(value: str) -> bool:
    candidate = value.strip().replace("\n", "").replace("\r", "").replace("\t", "")
    lowered = candidate.lower()
    if lowered.startswith("//"):  # protocol-relative is fine, it inherits https
        return True
    if lowered.startswith(("/", "#")):
        return True
    return lowered.startswith(SAFE_SCHEMES)


def _safe_style(value: str) -> str | None:
    lowered = value.lower()
    if any(token in lowered for token in CSS_BLOCKED):
        return None
    return value


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []
        self._suppress = 0  # depth inside a dropped-with-content element

    # -- helpers ---------------------------------------------------------------------
    def _clean_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = ALLOWED_ATTRS.get(tag, frozenset()) | GLOBAL_ATTRS
        parts: list[str] = []
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            value = raw_value or ""
            if name.startswith("on") or name not in allowed:
                continue
            if name in URL_ATTRS and not _safe_url(value):
                continue
            if name == "style":
                cleaned = _safe_style(value)
                if cleaned is None:
                    continue
                value = cleaned
            parts.append(f'{name}="{escape(value, quote=True)}"')
        if tag == "a":
            # Opening a link from an email must not hand the opener window to the target.
            parts = [p for p in parts if not p.startswith(("target=", "rel="))]
            parts.append('target="_blank"')
            parts.append('rel="noopener noreferrer nofollow"')
        return (" " + " ".join(parts)) if parts else ""

    # -- parser hooks ----------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._suppress:
            if tag in DROP_WITH_CONTENT:
                self._suppress += 1
            return
        if tag in DROP_WITH_CONTENT:
            self._suppress = 1
            return
        if tag not in ALLOWED_TAGS:
            return  # unwrap: the tag goes, its text stays
        self._out.append(f"<{tag}{self._clean_attrs(tag, attrs)}>")
        if tag not in VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._suppress or tag in DROP_WITH_CONTENT or tag not in ALLOWED_TAGS:
            return
        self._out.append(f"<{tag}{self._clean_attrs(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppress:
            if tag in DROP_WITH_CONTENT:
                self._suppress -= 1
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self._open:
            # Close anything the message left dangling, innermost first.
            while self._open:
                current = self._open.pop()
                self._out.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._out.append(escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        return  # conditional comments can smuggle markup

    def handle_decl(self, decl: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def result(self) -> str:
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)


def sanitize_html(html: str | None) -> str | None:
    """Return `html` reduced to the allow-list above, or None when there is nothing left."""
    if not html or not html.strip():
        return None
    parser = _Sanitizer()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # a malformed body must never take the request down
        return None
    cleaned = parser.result().strip()
    return cleaned or None


def html_to_text(html: str | None) -> str | None:
    """Crude text rendering, used only when a message ships no text/plain part."""
    if not html:
        return None

    class _Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag.lower() in DROP_WITH_CONTENT:
                self.skip += 1
            elif tag.lower() in ("br", "p", "div", "tr", "li"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag.lower() in DROP_WITH_CONTENT and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    stripper = _Text()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception:
        return None
    lines = [line.strip() for line in "".join(stripper.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None
