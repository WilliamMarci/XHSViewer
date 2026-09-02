"""Kitty graphics protocol support (works in Ghostty / kitty / tmux).

Uses *unicode placeholders* (U+10EEEE), the approach proven by yazi/ratatui:
transmit raw RGBA with U=1 (zlib compressed), then write placeholder cells
whose foreground color encodes the image id and whose combining diacritics
encode the (row, col) position.
"""
from __future__ import annotations

import base64
import os
import re
import select
import termios
import time
import tty
import zlib

_CHUNK = 3000  # base64 chars per chunk (safe under kitty's 4096 limit)

PLACEHOLDER = "\U0010eeee"

# kitty rowcolumn-diacritics.txt (kitty >= 0.28); index = row/col value
DIACRITICS = [
    "\u0305", "\u030d", "\u030e", "\u0310", "\u0312", "\u033d", "\u033e", "\u033f",
    "\u0346", "\u034a", "\u034b", "\u034c", "\u0350", "\u0351", "\u0352", "\u0357",
    "\u035b", "\u0363", "\u0364", "\u0365", "\u0366", "\u0367", "\u0368", "\u0369",
    "\u036a", "\u036b", "\u036c", "\u036d", "\u036e", "\u036f", "\u0483", "\u0484",
    "\u0485", "\u0486", "\u0487", "\u0592", "\u0593", "\u0594", "\u0595", "\u0597",
    "\u0598", "\u0599", "\u059c", "\u059d", "\u059e", "\u059f", "\u05a0", "\u05a1",
    "\u05a8", "\u05a9", "\u05ab", "\u05ac", "\u05af", "\u05c4", "\u0610", "\u0611",
    "\u0612", "\u0613", "\u0614", "\u0615", "\u0616", "\u0617", "\u0618", "\u0619",
    "\u061a", "\u0657", "\u0658", "\u0659", "\u065a", "\u065b", "\u065d", "\u065e",
    "\u065f", "\u06d6", "\u06d7", "\u06d8", "\u06d9", "\u06da", "\u06db", "\u06dc",
    "\u06df", "\u06e0", "\u06e1", "\u06e2", "\u06e4", "\u06e7", "\u06e8", "\u06eb",
    "\u06ec", "\u0730", "\u0732", "\u0733", "\u0735", "\u0736", "\u073a", "\u073d",
    "\u073f", "\u0741", "\u0743", "\u0745", "\u0747", "\u0749", "\u074a", "\u07eb",
    "\u07ec", "\u07ed", "\u07ee", "\u07ef", "\u07f0", "\u07f1", "\u07f3", "\u0816",
    "\u0817", "\u0818", "\u0819", "\u081b", "\u081c", "\u081d", "\u081e", "\u081f",
    "\u0820", "\u0821", "\u0822", "\u0823", "\u0825", "\u0826", "\u0827", "\u0829",
    "\u082a", "\u082b", "\u082c", "\u082d", "\u082e", "\u0951", "\u0953", "\u0954",
    "\u0f82", "\u0f83", "\u0f86", "\u0f87", "\u135d", "\u135e", "\u135f", "\u17dd",
    "\u193a", "\u1a17", "\u1a75", "\u1a76", "\u1a77", "\u1a78", "\u1a79", "\u1a7a",
    "\u1a7b", "\u1a7c", "\u1b6b", "\u1b6d", "\u1b6e", "\u1b6f", "\u1b70", "\u1b71",
    "\u1b72", "\u1b73", "\u1cd0", "\u1cd1", "\u1cd2", "\u1cda", "\u1cdb", "\u1ce0",
    "\u1dc0", "\u1dc1", "\u1dc3", "\u1dc4", "\u1dc5", "\u1dc6", "\u1dc7", "\u1dc8",
    "\u1dc9", "\u1dcb", "\u1dcc", "\u1dcd", "\u1dce", "\u1dcf", "\u1dd0", "\u1dd1",
    "\u1dd2", "\u1dd3", "\u1dd4", "\u1dd5", "\u1dd6", "\u1dd7", "\u1dd8", "\u1dd9",
    "\u1dda", "\u1ddb", "\u1ddc", "\u1ddd", "\u1dde", "\u1ddf", "\u1de0", "\u1de1",
    "\u1de2", "\u1de3", "\u1de4", "\u1de5", "\u1de6", "\u1de7", "\u1de8", "\u1de9",
    "\u1dea", "\u1deb", "\u1dec", "\u1ded", "\u1dee", "\u1def", "\u1dfe", "\u20d0",
    "\u20d1", "\u20d4", "\u20d5", "\u20d6", "\u20d7", "\u20db", "\u20dc", "\u20e1",
    "\u20e7", "\u20e9", "\u20f0", "\u2cef", "\u2cf0", "\u2cf1", "\u2de0", "\u2de1",
    "\u2de2", "\u2de3", "\u2de4", "\u2de5", "\u2de6", "\u2de7", "\u2de8", "\u2de9",
    "\u2dea", "\u2deb", "\u2dec", "\u2ded", "\u2dee", "\u2def", "\u2df0", "\u2df1",
    "\u2df2", "\u2df3", "\u2df4", "\u2df5", "\u2df6", "\u2df7", "\u2df8", "\u2df9",
    "\u2dfa", "\u2dfb", "\u2dfc", "\u2dfd", "\u2dfe", "\u2dff", "\ua66f", "\ua67c",
    "\ua67d", "\ua6f0", "\ua6f1", "\ua8e0", "\ua8e1", "\ua8e2", "\ua8e3", "\ua8e4",
    "\ua8e5", "\ua8e6", "\ua8e7", "\ua8e8", "\ua8e9", "\ua8ea", "\ua8eb", "\ua8ec",
    "\ua8ed", "\ua8ee", "\ua8ef", "\ua8f0", "\ua8f1", "\uaab0", "\uaab2", "\uaab3",
    "\uaab7", "\uaab8", "\uaabe", "\uaabf", "\uaac1", "\ufe20", "\ufe21", "\ufe22",
    "\ufe23", "\ufe24", "\ufe25", "\ufe26", "\u10a0f", "\u10a38", "\u1d185", "\u1d186",
    "\u1d187", "\u1d188", "\u1d189", "\u1d1aa", "\u1d1ab", "\u1d1ac", "\u1d1ad",
    "\u1d242", "\u1d243", "\u1d244",
]


# set by __main__ after probing the terminal
enabled = False


def terminal_supports_kitty() -> bool:
    prog = (os.environ.get("TERM_PROGRAM") or "").lower()
    term = (os.environ.get("TERM") or "").lower()
    forced = os.environ.get("ZHIHU_KITTY", "")
    if forced:
        return forced.lower() in ("1", "true", "yes")
    return "ghostty" in prog or "kitty" in prog or "kitty" in term or "ghostty" in term


def query_cell_size(timeout: float = 1.0) -> tuple[int, int] | None:
    """Query the terminal for its cell pixel size (kitty protocol q=2).

    Returns (cell_width, cell_height) in pixels, or None if the terminal
    does not answer.  Must be called before Textual takes over the TTY.
    """
    if not os.isatty(0) or not os.isatty(1):
        return None
    try:
        old = termios.tcgetattr(0)
    except Exception:
        return None
    buf = b""
    try:
        tty.setraw(0)
        os.write(1, b"\x1b_G q=2\x1b\\")
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([0], [], [], 0.1)
            if r:
                chunk = os.read(0, 4096)
                if not chunk:
                    break
                buf += chunk
                if b"\x1b\\" in buf:
                    break
    except Exception:
        return None
    finally:
        try:
            termios.tcsetattr(0, termios.TCSADRAIN, old)
        except Exception:
            pass
    text = buf.decode("ascii", "ignore")
    # preferred: explicit cell size
    m = re.search(r"cw=(\d+);ch=(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    # fallback: derive from screen pixel size / character cells
    m = re.search(r"s=(\d+);v=(\d+);c=(\d+);r=(\d+)", text)
    if m and int(m.group(3)) > 0 and int(m.group(4)) > 0:
        return (
            max(1, int(m.group(1)) // int(m.group(3))),
            max(1, int(m.group(2)) // int(m.group(4))),
        )
    # terminal answered the query but sent no size info
    if "ok" in text:
        return 8, 16
    return None


def probe(timeout: float = 1.0) -> tuple[tuple[int, int], bool]:
    """Probe the terminal for kitty graphics support.

    Returns ((cell_w, cell_h), kitty_supported).  A terminal that merely
    *claims* to be ghostty/kitty is trusted even if the query fails, so
    images keep working there (with a default cell size).
    """
    cell = query_cell_size(timeout)
    if cell is not None:
        return cell, True
    return (8, 16), terminal_supports_kitty()


# --------------------------------------------------------------------------
# image transmission (unicode placeholder mode, yazi-proven)
# --------------------------------------------------------------------------


def image_id_color(img_id: int) -> tuple[int, int, int]:
    return ((img_id >> 16) & 0xFF, (img_id >> 8) & 0xFF, img_id & 0xFF)


def image_id_style(img_id: int) -> str:
    r, g, b = image_id_color(img_id)
    return f"#{r:02x}{g:02x}{b:02x}"


def transmit_rgba(rgba: bytes, img_id: int, w_px: int, h_px: int) -> str:
    """Transmit raw RGBA pixels with U=1 unicode placement (zlib compressed).

    No placement is created here; the image is displayed by writing
    PLACEHOLDER + diacritic cells afterwards.
    """
    raw = zlib.compress(rgba)
    b64 = base64.b64encode(raw).decode("ascii")
    out: list[str] = []
    n = len(b64)
    for i in range(0, n, _CHUNK):
        data = b64[i : i + _CHUNK]
        more = 1 if i + _CHUNK < n else 0
        if i == 0:
            params = f"q=2,a=T,C=1,U=1,f=32,s={w_px},v={h_px},i={img_id},m={more}"
        else:
            params = f"m={more}"
        out.append(f"\x1b_G{params};{data}\x1b\\")
    return "".join(out)


def placeholder_cells(img_id: int, cols: int, rows: int) -> str:
    """Plain-SGR placeholder grid (for direct stdout output, e.g. the demo).

    The foreground color encodes the image id; the two combining diacritics
    per cell encode the (row, col) position within the image box.
    """
    r, g, b = image_id_color(img_id)
    out = [f"\x1b[38;2;{r};{g};{b}m"]
    n = len(DIACRITICS)
    for y in range(rows):
        for x in range(cols):
            out.append(f"{PLACEHOLDER}{DIACRITICS[y % n]}{DIACRITICS[x % n]}")
        out.append("\n")
    out.append("\x1b[39m")
    return "".join(out)


def delete_image(img_id: int) -> str:
    return f"\x1b_Gq=2,a=d,d=i,i={img_id}\x1b\\"


def delete_all_images() -> str:
    return "\x1b_Gq=2,a=d,d=A\x1b\\"