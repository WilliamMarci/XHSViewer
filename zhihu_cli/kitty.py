"""Kitty graphics protocol support (works in Ghostty).

Implements image transmit/place/delete escape sequences plus a one-shot
cell-size query, all written directly to the terminal driver.
"""
from __future__ import annotations

import base64
import os
import re
import select
import termios
import time
import tty
from io import BytesIO

from PIL import Image as PILImage

_CHUNK = 3000  # base64 chars per chunk (safe under kitty's 4096 limit)

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


def prepare_png(
    image_data: bytes, max_width_px: int
) -> tuple[bytes, int, int]:
    """Downscale image if wider than max_width_px and re-encode as PNG.

    Returns (png_bytes, width_px, height_px).
    """
    im = PILImage.open(BytesIO(image_data))
    if im.width > max_width_px:
        im = im.resize(
            (max_width_px, max(1, int(im.height * max_width_px / im.width))),
            PILImage.LANCZOS,
        )
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    out = BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue(), im.width, im.height


def emit_image(png: bytes, img_id: str, w_px: int, h_px: int) -> str:
    """Build the escape sequence that transmits + places an image at the
    current cursor position (1 pixel = 1 cell, capped by terminal cell size).

    Returns a str (pure ASCII), safe to pass to the terminal driver.
    """
    b64 = base64.b64encode(png)
    out: list[str] = []
    n = len(b64)
    for i in range(0, n, _CHUNK):
        data = b64[i : i + _CHUNK].decode("ascii")
        more = 1 if i + _CHUNK < n else 0
        params = f"a=T,f=100,s={w_px},v={h_px},p=1,q=1,i={img_id},m={more}"
        out.append(f"\x1b_G{params};{data}\x1b\\")
    return "".join(out)


def delete_image(img_id: str) -> str:
    return f"\x1b_Ga=d,d=i,i={img_id}\x1b\\"


def delete_all_images() -> str:
    return "\x1b_Ga=d,d=A\x1b\\"