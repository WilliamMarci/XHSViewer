"""Parse zhihu answer/article HTML into styled rich Text blocks.

Element -> style mapping (color / bold / background):
  h1-h3     bold bright cyan
  blockquote italic light-blue text on dark blue background
  pre/code  amber text on dark background
  a         blue + underline
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
from rich.text import Text

from .mathtex import render_math

BASE = "https://www.zhihu.com"

H_COLOR = "#4fc3f7"          # headings
QUOTE_COLOR = "#b8c9e0"      # blockquote text
QUOTE_BG = "#1d2b3f"         # blockquote background
CODE_COLOR = "#e6b450"       # code text
CODE_BG = "#20222a"          # code background
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


def img_src(img: Tag) -> str:
    for attr in ("data-actualsrc", "data-original", "data-src", "src"):
        u = (img.get(attr) or "").strip()
        if u and not u.startswith("data:"):
            return urljoin(BASE, u)
    return ""


def is_equation(img: Tag) -> bool:
    """True if the img is a math formula (zhihu equation image)."""
    src = (img.get("src") or "").lower()
    if "equation" in src or "formula" in src:
        return True
    cls = (img.get("class") or "").lower()
    if "formula" in cls or "equation" in cls:
        return True
    alt = img.get("alt") or ""
    return "\\" in alt


def is_inline_equation(img: Tag) -> bool:
    """eeimg='1' marks inline formulas; block formulas have no eeimg.

    Multi-line constructs (\begin{...}) are always treated as display math.
    """
    if img.get("eeimg") != "1":
        return False
    return "\\begin{" not in equation_latex(img)


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

    Inline formulas stay single-line inside the paragraph; display formulas
    become their own (possibly multi-line, utftex-typeset) block.
    """
    latex = equation_latex(img)
    out = render_math(latex, inline=inline)
    if not out.strip():
        return Text("", style=MATH_COLOR)
    if inline:
        return Text(out, style=f"italic {MATH_COLOR}")
    return Text(out, style=f"{MATH_COLOR}")


def inline(node: Tag) -> Text:
    """Render inline content of a tag into a rich Text."""
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
                    # inline formulas stay in the paragraph flow
                    t.append_text(math_text(child, inline=True))
                    continue
                # display formulas are extracted by the block walker
                if not is_inline_equation(child):
                    continue
                u = img_src(child)
                if u:
                    t.append(" [图片] ", style=f"bold {LINK_COLOR}")
                continue
            sub = inline(child)
            style = _INLINE_STYLE.get(name)
            if style:
                sub.stylize(style)
            t.append_text(sub)
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
        for child in node.children:
            if isinstance(child, NavigableString):
                s = str(child).strip()
                if s:
                    blocks.append(Block("p", Text(s)))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in _SKIP_TAGS:
                continue
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                t = inline(child)
                t.stylize(f"bold {H_COLOR}" if name in ("h1", "h2", "h3") else "bold")
                blocks.append(Block("h", t))
            elif name == "p":
                imgs = [img_src(i) for i in child.find_all("img")]
                display_eqs = [
                    i
                    for i in child.find_all("img")
                    if is_equation(i) and not is_inline_equation(i)
                ]
                t = inline(child)
                plain = t.plain.replace("[图片]", "").strip()
                if plain:
                    blocks.append(Block("p", t))
                # display formulas become their own centered block
                for eq in display_eqs:
                    blocks.append(Block("math", math_text(eq, inline=False)))
                if imgs:
                    add_img(imgs)
            elif name == "blockquote":
                t = inline(child)
                if t.plain.strip():
                    t.stylize(f"italic {QUOTE_COLOR} on {QUOTE_BG}")
                    blocks.append(Block("quote", t))
            elif name == "pre":
                t = Text(child.get_text(), style=f"{CODE_COLOR} on {CODE_BG}")
                blocks.append(Block("code", t))
            elif name in ("ul", "ol"):
                items: list[Text] = []
                ordered = name == "ol"
                for i, li in enumerate(child.find_all("li", recursive=False), 1):
                    marker = f"{i}. " if ordered else "• "
                    t = Text(marker, style=f"bold {BULLET_COLOR}")
                    t.append_text(inline(li))
                    items.append(t)
                blocks.append(Block("list", items=items))
            elif name == "img":
                add_img([img_src(child)], caption=(child.get("alt") or "").strip())
            elif name == "figure":
                imgs = [img_src(i) for i in child.find_all("img")]
                cap = child.find("figcaption")
                caption = inline(cap).plain.strip() if cap else ""
                add_img(imgs, caption)
                if cap and caption:
                    blocks.append(Block("figcap", Text(caption, style=f"dim {DIM}")))
            elif name == "hr":
                blocks.append(Block("hr", Text("━" * 16, style=f"dim {DIM}")))
            elif name == "table":
                rows: list[list[str]] = []
                for tr in child.find_all("tr"):
                    cells = [
                        inline(td).plain.strip() for td in tr.find_all(["td", "th"])
                    ]
                    rows.append(cells)
                blocks.append(Block("table", rows=rows))
            elif name in ("div", "section", "center", "article", "details", "summary"):
                walk(child)
            else:
                t = inline(child)
                if t.plain.strip():
                    blocks.append(Block("p", t))

    body = soup.body or soup
    walk(body)
    return blocks, images


def table_to_text(block: Block) -> Text:
    t = Text()
    for i, row in enumerate(block.rows):
        cells = [c.replace("\n", " ").strip() for c in row]
        line = " │ ".join(cells)
        if i == 0:
            t.append(line, style="bold")
        else:
            t.append(line)
        t.append("\n")
    if t.plain.endswith("\n"):
        t.plain = t.plain.rstrip("\n")
    return t