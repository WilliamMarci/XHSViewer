"""Parse zhihu answer/article HTML into styled rich Text blocks.

Element -> style mapping (color / bold / background):
  h1-h3     bold bright cyan
  blockquote italic text (block styling belongs to CSS)
  pre/code  semantic text coloring (block styling belongs to CSS)
  a         themed background + underline in the UI renderer
  strong/b  bold        em/i italic        del/s strikethrough dim
  ul/ol     colored bullets / numbers
  table     cells joined with vertical bars, header bold
  hr        dim divider      figcaption dim
  img       collected into img blocks (rendered as expandable image sections)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup, NavigableString, Tag
from rich.cells import cell_len
from rich.text import Text

from . import config
from .mathtex import render_math

BASE = "https://www.zhihu.com"

H_COLOR = "#4fc3f7"          # headings
# per-level heading colors (h1 主标题 / h2 次标题 / h3 小节)
H_COLORS = {"h1": "#4fc3f7", "h2": "#a78bfa", "h3": "#34d399"}
QUOTE_COLOR = "#b8c9e0"      # blockquote text
CODE_COLOR = "#e6b450"       # code text
CODE_BG = "#20222a"          # code background

# --- semantic coloring for <pre> code blocks -------------------------
_CODE_BASE = "#d7dee8"
_CODE_KW = "#f97583"
_CODE_STR = "#7ee787"
_CODE_COM = "#7d8590"
_CODE_NUM = "#e6b450"
_CODE_FN = "#82aaff"
_CODE_KW_SET = {
    "def", "class", "if", "elif", "else", "for", "while", "return", "import",
    "from", "as", "try", "except", "finally", "raise", "with", "lambda",
    "pass", "break", "continue", "yield", "global", "nonlocal", "del",
    "and", "or", "not", "in", "is", "None", "True", "False", "async", "await",
    "function", "const", "let", "var", "new", "this", "typeof", "of",
    "ifdef", "endif", "include", "pragma", "struct", "enum", "union",
    "typedef", "static", "extern", "register", "volatile", "goto", "sizeof",
    "switch", "case", "default", "do", "int", "float", "double", "char",
    "void", "long", "short", "unsigned", "signed", "bool", "string",
    "fn", "pub", "mut", "impl", "trait", "mod", "use", "let", "match",
    "package", "select", "where", "order", "group", "by", "having",
    "insert", "update", "delete", "into", "values", "set", "join",
    "inner", "outer", "limit", "offset", "create", "table", "drop",
    "alter", "add", "primary", "key", "foreign", "references", "unique",
    "index", "begin", "commit", "rollback", "and", "or", "not", "null",
    "print", "echo", "require", "json", "true", "false",
}
_CODE_RE = re.compile(
    r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`)"""  # string
    r"""|(//[^\n]*|#[^\n]*|/\*.*?\*/|--[^\n]*)"""           # comment
    r"""|(\b\d[\w.]*\b)"""                                  # number
    r"""|([A-Za-z_][\w]*(?=\s*\())"""                       # function call
    r"""|([A-Za-z_][\w]*)"""                                # word
)


# Subtle highlight for the matched portion of search results (<em>…</em>)
SEARCH_HILITE = "bold #fbbf24"


def parse_highlighted(html: str, base: str = "") -> Text:
    """Parse a small HTML snippet into a Text, highlighting <em> spans.

    Used for search result titles/excerpts: zhihu wraps the matched part
    of the query in <em>, which is rendered with a subtle highlight while
    the rest of the text keeps ``base``.
    """
    soup = BeautifulSoup(html or "", "lxml")

    def walk(node) -> Text:
        out = Text()
        for child in node.children:
            if isinstance(child, NavigableString):
                out.append(str(child), style=base or None)
            elif isinstance(child, Tag):
                name = child.name
                if name in _SKIP_TAGS or name in ("script", "style"):
                    continue
                if name == "br":
                    out.append("\n")
                    continue
                if name == "em":
                    out.append(child.get_text(), style=SEARCH_HILITE)
                    continue
                out.append_text(walk(child))
        return out

    return walk(soup)


def highlight_code(code: str) -> Text:
    """Lightweight semantic highlighting for code blocks."""
    t = Text(style=_CODE_BASE)
    last = 0
    for m in _CODE_RE.finditer(code):
        if m.start() > last:
            t.append(code[last : m.start()])
        s, com, num, fn, word = m.groups()
        if s is not None:
            t.append(s, style=_CODE_STR)
        elif com is not None:
            t.append(com, style=f"italic {_CODE_COM}")
        elif num is not None:
            t.append(num, style=_CODE_NUM)
        elif fn is not None:
            t.append(fn, style=_CODE_FN)
        elif word is not None:
            if word in _CODE_KW_SET:
                t.append(word, style=f"bold {_CODE_KW}")
            else:
                t.append(word)
        last = m.end()
    if last < len(code):
        t.append(code[last:])
    return t
LINK_COLOR = "#5eb4f8"       # links
BULLET_COLOR = "#4fc3f7"     # list markers
MATH_COLOR = "#c792ea"       # math formulas
DIM = "#8a93a3"

_INLINE_STYLE = {
    "strong": "bold",
    "b": "bold",
    "em": "italic",
    "i": "italic",
    "del": "strike dim",
    "s": "strike dim",
    "u": "underline",
    "a": f"underline {LINK_COLOR}",
    "code": f"{CODE_COLOR} on {CODE_BG}",
    "sub": "dim",
    "sup": "dim",
}

_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "video", "audio"}


@dataclass
class Block:
    kind: str  # p | h | quote | code | list | table | img | hr | figcap | note
    text: Text | None = None
    urls: list[str] = field(default_factory=list)
    items: list[Text] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    caption: str = ""
    lang: str = ""  # code language (e.g. python) for <pre> blocks
    # clickable link spans: (start, end, url) into self.text.plain
    links: list[tuple[int, int, str]] = field(default_factory=list)
    # per-item link spans for list blocks (offsets relative to each item)
    item_links: list[list[tuple[int, int, str]]] = field(default_factory=list)


def img_src(img: Tag) -> str:
    for attr in ("data-actualsrc", "data-original", "data-src", "src"):
        u = (img.get(attr) or "").strip()
        if u and not u.startswith("data:"):
            return urljoin(BASE, u)
    return ""


def _classes(img: Tag) -> str:
    cls = img.get("class") or []
    if not isinstance(cls, str):
        cls = " ".join(cls)
    return cls


def is_equation(img: Tag) -> bool:
    """True if the img is a math formula (zhihu equation image)."""
    src = (img.get("src") or "").lower()
    if "equation" in src or "formula" in src:
        return True
    if "formula" in _classes(img).lower() or "equation" in _classes(img).lower():
        return True
    alt = img.get("alt") or ""
    return "\\" in alt


def is_formula_svg(img: Tag) -> bool:
    """True if the img is an SVG formula (display equations zhihu renders
    as SVG, which terminals cannot show)."""
    src = (img.get("src") or "").lower()
    return ("svg" in src or "svg+xml" in src) and is_equation(img)


def formula_img_visible(img: Tag) -> bool:
    """Whether a formula image should also be offered as a viewable image."""
    if not is_formula_svg(img):
        return True
    return bool(config.get("show_math_svg", False))


def is_inline_equation(img: Tag) -> bool:
    """Zhihu's ``eeimg=1`` is authoritative for inline equations.

    Inline matrices/cases are flattened by :mod:`mathtex`; formulas without
    the marker retain utftex's multi-line display rendering.
    """
    return img.get("eeimg") == "1"


def equation_latex(img: Tag) -> str:
    alt = (img.get("alt") or "").strip()
    if alt:
        return alt
    src = img.get("src") or ""
    m = re.search(r"[?&]tex=([^&]+)", src)
    if m:
        return unquote(m.group(1))
    return ""


def math_text(img: Tag, inline: bool) -> Text:
    """Convert a formula img to a styled rich Text.

    Inline formulas stay single-line inside the paragraph; display
    formulas become their own (utftex-typeset, possibly multi-line)
    block — utftex CLI is authoritative for display math.
    """
    latex = equation_latex(img)
    out = render_math(latex, inline=inline)
    if not out.strip():
        return Text("", style=MATH_COLOR)
    if inline:
        return Text(out, style=f"italic {MATH_COLOR}")
    return Text(out, style=f"{MATH_COLOR}")


# Strict URL matcher (http/https).  Matches URLs even when they appear as
# plain text (not wrapped in <a>), then trims trailing punctuation.
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"'“”‘’]+")
_URL_TRAIL = set(".,;:!?。，；：！？…)]}\"'")


def find_urls(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, url) for every URL found in ``text``."""
    out: list[tuple[int, int, str]] = []
    for m in _URL_RE.finditer(text):
        s, e = m.start(), m.end()
        url = text[s:e]
        while url and url[-1] in _URL_TRAIL:
            url = url[:-1]
            e -= 1
        while url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1]
            e -= 1
        if url and "http://" != url and "https://" != url:
            out.append((s, e, url))
    return out


def _code_lang(tag: Tag) -> str:
    """Best-effort language detection from <pre>/<code> class names."""
    for t in (tag, *tag.find_all("code")):
        cls = _classes(t)
        for m in re.findall(r"(?:language|lang)[-_]([\w+#]+)", cls, re.I):
            return m
    return ""


def inline(node: Tag, links: list[tuple[int, int, str]] | None = None) -> Text:
    """Render inline content of a tag into a rich Text.

    If ``links`` is given, ``(start, end, url)`` tuples for every ``<a>``
    are appended (offsets into the returned Text's plain text).
    """
    t = Text()
    for child in node.children:
        if isinstance(child, NavigableString):
            t.append(str(child))
        elif isinstance(child, Tag):
            name = child.name
            if name in _SKIP_TAGS:
                continue
            if name == "br":
                t.append("\n")
                continue
            if name == "img":
                if is_equation(child):
                    # Only explicitly inline formulas belong in paragraph
                    # flow. Display formulas are emitted by the block walker.
                    if is_inline_equation(child):
                        t.append_text(math_text(child, inline=True))
                    continue
                # display formulas are extracted by the block walker
                if not is_inline_equation(child):
                    continue
                u = img_src(child)
                if u:
                    t.append(" [图片] ", style=f"bold {LINK_COLOR}")
                continue
            if name == "sup":
                # Inspect first without exporting nested anchor offsets: a
                # reference marker is rewritten below and its old offsets no
                # longer correspond to the parent text.
                sub = inline(child, None)
                classes = _classes(child).lower()
                href = ""
                anchor = child.find("a")
                if anchor is not None:
                    href = (anchor.get("href") or "").strip()
                is_reference = (
                    "reference" in classes
                    or "footnote" in classes
                    or any(child.get(key) for key in ("data-numero", "data-number", "data-index"))
                    or bool(re.search(r"(?:#|/)(?:ref|footnote)[-_]?\d+", href, re.I))
                )
                if is_reference:
                    number = (
                        child.get("data-numero")
                        or child.get("data-number")
                        or child.get("data-index")
                        or sub.plain.strip().strip("[]")
                    )
                    if not number:
                        match = re.search(r"(\d+)", href)
                        number = match.group(1) if match else "?"
                    sub = Text(f"[{number}]", style="bold")
                else:
                    sub.stylize("dim")
                t.append_text(sub)
                continue
            if name == "a":
                sub = inline(child, None)
                href = (child.get("href") or "").strip()
                # Zhihu link-card anchors are intentionally empty; their
                # visible label lives in data-text instead of textContent.
                if not sub.plain.strip():
                    label = (
                        child.get("data-text")
                        or child.get("title")
                        or child.get("aria-label")
                        or ""
                    ).strip()
                    if label:
                        sub = Text(label)
                if href:
                    start = len(t.plain)
                    sub.stylize(f"underline {LINK_COLOR}")
                    t.append_text(sub)
                    end = len(t.plain)
                    if links is not None:
                        links.append((start, end, urljoin(BASE, href)))
                    continue
                t.append_text(sub)
                continue
            if name == "code":
                # Inline code must stay inline: collapse any whitespace /
                # newlines (HTML source may put the code text on its own line).
                code_links: list[tuple[int, int, str]] = []
                sub = inline(child, code_links)
                plain = re.sub(r"\s+", " ", sub.plain).strip(" ")
                sub = Text(plain, style=f"{CODE_COLOR} on {CODE_BG}")
                offset = len(t.plain)
                t.append_text(sub)
                if links is not None:
                    links.extend((s + offset, e + offset, url) for s, e, url in code_links)
                continue
            child_links: list[tuple[int, int, str]] = []
            sub = inline(child, child_links)
            style = _INLINE_STYLE.get(name)
            if style:
                sub.stylize(style)
            offset = len(t.plain)
            t.append_text(sub)
            if links is not None:
                links.extend((s + offset, e + offset, url) for s, e, url in child_links)
    return t


def parse_html(html: str) -> tuple[list[Block], list[str]]:
    """Return (blocks, all_image_urls) for a content html string."""
    soup = BeautifulSoup(html or "", "lxml")
    blocks: list[Block] = []
    images: list[str] = []

    def add_img(urls: list[str], caption: str = ""):
        urls = [u for u in urls if u]
        if not urls:
            return
        for u in urls:
            if u not in images:
                images.append(u)
        blocks.append(Block("img", urls=urls, caption=caption))

    def walk(node: Tag):
        # Consecutive inline children (text, <code>, <a>, <strong>, <span>…)
        # are accumulated into one paragraph so inline code stays inline
        # instead of becoming its own block.
        pending: Text | None = None
        pending_links: list[tuple[int, int, str]] = []

        def flush():
            nonlocal pending, pending_links
            if pending is not None and pending.plain.strip():
                blocks.append(Block("p", pending, links=pending_links))
            pending = None
            pending_links = []

        for child in node.children:
            if isinstance(child, NavigableString):
                s = str(child).strip()
                if not s:
                    continue
                if pending is None:
                    pending = Text()
                pending.append(s)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in _SKIP_TAGS:
                continue
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                flush()
                h_links: list[tuple[int, int, str]] = []
                t = inline(child, h_links)
                color = H_COLORS.get(name)
                t.stylize(f"bold {color}" if color else "bold")
                blocks.append(Block("h", t, links=h_links))
            elif name == "p":
                flush()
                imgs = [
                    img_src(i)
                    for i in child.find_all("img")
                    if not is_equation(i) and formula_img_visible(i)
                ]
                display_eqs = [
                    i
                    for i in child.find_all("img")
                    if is_equation(i) and not is_inline_equation(i)
                ]
                p_links: list[tuple[int, int, str]] = []
                t = inline(child, p_links)
                plain = t.plain.replace("[图片]", "").strip()
                if plain:
                    blocks.append(Block("p", t, links=p_links))
                # display formulas become their own centered block
                for eq in display_eqs:
                    blocks.append(Block("math", math_text(eq, inline=False)))
                if imgs:
                    add_img(imgs)
            elif name == "blockquote":
                flush()
                q_links: list[tuple[int, int, str]] = []
                t = inline(child, q_links)
                if t.plain.strip():
                    t.stylize(f"italic {QUOTE_COLOR}")
                    blocks.append(Block("quote", t, links=q_links))
            elif name == "pre":
                flush()
                t = highlight_code(child.get_text())
                blocks.append(Block("code", t, lang=_code_lang(child)))
            elif name in ("ul", "ol"):
                flush()
                items: list[Text] = []
                item_links: list[list[tuple[int, int, str]]] = []
                ordered = name == "ol"
                for i, li in enumerate(child.find_all("li", recursive=False), 1):
                    marker = f"{i}. " if ordered else "• "
                    li_links: list[tuple[int, int, str]] = []
                    t = Text(marker, style=f"bold {BULLET_COLOR}")
                    t.append_text(inline(li, li_links))
                    off = len(marker)
                    item_links.append([(s + off, e + off, u) for s, e, u in li_links])
                    items.append(t)
                blocks.append(Block("list", items=items, item_links=item_links))
            elif name == "img":
                flush()
                if is_equation(child):
                    # display (block) formula -> math text block
                    blocks.append(Block("math", math_text(child, inline=False)))
                elif not formula_img_visible(child):
                    pass  # hidden SVG formula
                else:
                    add_img([img_src(child)], caption=(child.get("alt") or "").strip())
            elif name == "figure":
                flush()
                eqs = [
                    i
                    for i in child.find_all("img")
                    if is_equation(i) and not is_inline_equation(i)
                ]
                imgs = [
                    img_src(i)
                    for i in child.find_all("img")
                    if not is_equation(i) and formula_img_visible(i)
                ]
                cap = child.find("figcaption")
                cap_links: list[tuple[int, int, str]] = []
                caption = inline(cap, cap_links).plain.strip() if cap else ""
                for eq in eqs:
                    blocks.append(Block("math", math_text(eq, inline=False)))
                add_img(imgs, caption)
                if cap and caption:
                    blocks.append(
                        Block("figcap", Text(caption, style=f"dim {DIM}"), links=cap_links)
                    )
            elif name == "hr":
                flush()
                blocks.append(Block("hr", Text("━" * 16, style=f"dim {DIM}")))
            elif name == "table":
                flush()
                rows: list[list[str]] = []
                for tr in child.find_all("tr"):
                    cells = [
                        inline(td).plain.strip() for td in tr.find_all(["td", "th"])
                    ]
                    rows.append(cells)
                blocks.append(Block("table", rows=rows))
            elif name == "a":
                # Block link cards are top-level empty anchors. Calling
                # inline(child) would only inspect their (empty) children.
                flush()
                label = (
                    child.get_text(strip=True)
                    or child.get("data-text")
                    or child.get("title")
                    or child.get("aria-label")
                    or ""
                ).strip()
                href = (child.get("href") or "").strip()
                if label:
                    text = Text(label, style=f"underline {LINK_COLOR}")
                    links = [(0, len(label), urljoin(BASE, href))] if href else []
                    blocks.append(Block("p", text, links=links))
            elif name in ("div", "section", "center", "article", "details", "summary"):
                flush()
                walk(child)
            else:
                # inline element (code/strong/em/span/br/…): merge into the
                # current paragraph run
                sub_links: list[tuple[int, int, str]] = []
                sub = inline(child, sub_links)
                if not sub.plain.strip():
                    continue
                if pending is None:
                    pending = Text()
                base = len(pending.plain)
                for s, e, u in sub_links:
                    pending_links.append((base + s, base + e, u))
                pending.append_text(sub)

        flush()

    body = soup.body or soup
    walk(body)
    return blocks, images


TABLE_BORDER = "#2a3140"
TABLE_MAX_COL = 36  # per-column width cap; long cells wrap across lines


def _wrap_text(s: str, width: int) -> list[str]:
    """Wrap a cell string to ``width`` terminal columns (cell-width aware)."""
    s = s.replace("\n", " ").strip()
    out: list[str] = []
    cur = ""
    cur_w = 0
    for ch in s:
        w = cell_len(ch)
        if cur and cur_w + w > width:
            out.append(cur)
            cur, cur_w = "", 0
        cur += ch
        cur_w += w
    if cur or not out:
        out.append(cur)
    return out


def table_to_text(block: Block) -> Text:
    """Render a table as an aligned, box-drawn block.

    Column widths are computed cell-width aware (CJK friendly), long cells
    wrap onto extra lines, and the header row is bold.  Border lines use a
    dim color so they stay unobtrusive.
    """
    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in block.rows]
    if not rows:
        return Text("")
    ncols = max(len(r) for r in rows)
    norm = [r + [""] * (ncols - len(r)) for r in rows]

    widths: list[int] = []
    for c in range(ncols):
        w = max((cell_len(r[c]) for r in norm), default=0)
        widths.append(max(1, min(w, TABLE_MAX_COL)))

    cells = [[_wrap_text(r[c], widths[c]) for c in range(ncols)] for r in norm]

    def border(left: str, mid: str, right: str) -> Text:
        line = left + mid.join("─" * (w + 2) for w in widths) + right
        return Text(line, style=f"dim {TABLE_BORDER}")

    def row_lines(r: int, header: bool = False) -> list[Text]:
        max_lines = max(len(cells[r][c]) for c in range(ncols))
        out: list[Text] = []
        for li in range(max_lines):
            parts = []
            for c in range(ncols):
                txt = cells[r][c][li] if li < len(cells[r][c]) else ""
                pad = " " * (widths[c] - cell_len(txt))
                parts.append(f" {txt}{pad} ")
            out.append(Text("│".join(parts), style="bold" if header else None))
        return out

    t = Text()
    t.append_text(border("┌", "┬", "┐"))
    t.append("\n")
    for ln in row_lines(0, header=True):
        t.append_text(ln)
        t.append("\n")
    t.append_text(border("├", "┼", "┤"))
    t.append("\n")
    for r in range(1, len(cells)):
        for ln in row_lines(r):
            t.append_text(ln)
            t.append("\n")
    t.append_text(border("└", "┴", "┘"))
    return t
