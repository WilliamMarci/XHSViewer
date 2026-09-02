#!/usr/bin/env python3
"""独立图片显示演示（Kitty 图片协议，支持 Ghostty）。

仅负责在终端中显示图片，用于确认图片可以正常打开。

用法：
    python demo_image.py [图片文件或URL]        # 默认生成一张测试图
    python demo_image.py                        # 内置测试图（渐变+文字）

按键：
    + / -      放大 / 缩小
    h j k l    平移（或方向键）
    o          在浏览器中打开（URL 时）
    q / Esc    退出
"""
from __future__ import annotations

import io
import os
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zhihu_cli import kitty  # noqa: E402


def load_image(arg: str | None) -> tuple[bytes, str]:
    """Return (image_bytes, source_description)."""
    if arg is None:
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (800, 500), (20, 26, 40))
        draw = ImageDraw.Draw(im)
        for i, (c1, c2) in enumerate(
            [
                ((59, 130, 246), (16, 185, 129)),
                ((244, 63, 94), (167, 139, 250)),
                ((245, 158, 11), (56, 189, 248)),
            ]
        ):
            y0 = i * 167
            for y in range(167):
                t = y / 167
                draw.line(
                    [(0, y0 + y), (799, y0 + y)],
                    fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)),
                )
        draw.text((20, 30), "Zhihu Viewer 图片演示", fill="white")
        draw.text((20, 60), "+ / - 缩放   hjkl 平移   q 退出", fill=(200, 200, 200))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue(), "内置测试图"
    if arg.startswith(("http://", "https://")):
        import requests

        r = requests.get(arg, timeout=20, headers={"Referer": "https://www.zhihu.com/"})
        r.raise_for_status()
        return r.content, arg
    return Path(arg).read_bytes(), arg


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None

    if not kitty.terminal_supports_kitty() and not os.environ.get("ZHIHU_KITTY"):
        print("提示：未检测到 Kitty 兼容终端（如 Ghostty）。", file=sys.stderr)
        print("若你确信终端支持，可设置 ZHIHU_KITTY=1 强制启用。", file=sys.stderr)
        return 1

    cell = kitty.query_cell_size()
    if cell is None:
        cell = (8, 16)
    cw, ch = cell
    print(f"终端单元尺寸: {cw}x{ch}px，检测图片协议…", file=sys.stderr)

    data, desc = load_image(arg)
    from PIL import Image as PILImage

    im = PILImage.open(io.BytesIO(data))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    print(f"图片: {im.size[0]}x{im.size[1]}px  ({desc})", file=sys.stderr)

    img_id = "demo1"
    scale = 1.0
    pan_x = 0.0
    pan_y = 0.0

    old = termios.tcgetattr(0)
    try:
        tty.setraw(0)

        def write(s: str) -> None:
            os.write(1, s.encode("utf-8", "ignore"))

        def viewport() -> tuple[int, int]:
            import shutil

            cols, rows = shutil.get_terminal_size()
            return max(1, (cols - 2) * cw), max(1, (rows - 4) * ch)

        def emit() -> None:
            nonlocal pan_x, pan_y
            vw, vh = viewport()
            w, h = im.size
            fit = min(vw / w, vh / h, 1.0)
            dw = max(1, int(w * fit * scale))
            dh = max(1, int(h * fit * scale))
            sview_w = min(dw, vw)
            sview_h = min(dh, vh)
            px = min(max(0, dw - sview_w), max(0, int(pan_x)))
            py = min(max(0, dh - sview_h), max(0, int(pan_y)))
            cols = max(1, sview_w // cw)
            rows = max(1, sview_h // ch)
            resized = im.resize((dw, dh), PILImage.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, "PNG", optimize=True)
            png = buf.getvalue()
            write(kitty.transmit_image(png, img_id, dw, dh))
            import shutil

            tcols, trows = shutil.get_terminal_size()
            row = max(0, (trows - rows) // 2 - 1)
            col = max(0, (tcols - cols) // 2)
            write(f"\x1b[{row + 1};{col + 1}H" + kitty.place_image(
                img_id, px, py, sview_w, sview_h, cols, rows
            ))

        def cleanup() -> None:
            try:
                write(kitty.delete_image(img_id))
            except Exception:
                pass

        write("\x1b[2J\x1b[H")  # clear screen
        emit()
        print(f"\x1b[{1};1H\x1b[K 图片演示: 按 + / - 缩放，hjkl 平移，q 退出", end="", flush=True)
        write("\x1b[2;1H\x1b[K")

        while True:
            buf_in = os.read(0, 16)
            if not buf_in:
                break
            s = buf_in.decode("utf-8", "ignore")
            if s in ("q", "Q", "\x1b"):
                break
            if s in ("+", "="):
                scale = min(8.0, scale * 1.5)
                emit()
            elif s == "-":
                scale = max(0.4, scale / 1.5)
                emit()
            elif s in ("h", "\x1b[D"):
                pan_x -= 2 * cw
                emit()
            elif s in ("l", "\x1b[C"):
                pan_x += 2 * cw
                emit()
            elif s in ("k", "\x1b[A"):
                pan_y -= 2 * ch
                emit()
            elif s in ("j", "\x1b[B"):
                pan_y += 2 * ch
                emit()
            elif s == "o" and arg and arg.startswith("http"):
                import webbrowser

                cleanup()
                write("\x1b[2J\x1b[H")
                termios.tcsetattr(0, termios.TCSADRAIN, old)
                webbrowser.open(arg)
                print(f"已在浏览器打开: {arg}")
                return 0
            print(
                f"\x1b[1;1H\x1b[K 图片演示: 缩放 {scale:.2f}x  ({desc})  q 退出",
                end="",
                flush=True,
            )
            write("\x1b[2;1H\x1b[K")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            write(kitty.delete_image(img_id))
        except Exception:
            pass
        try:
            write("\x1b[2J\x1b[H")
        except Exception:
            pass
        termios.tcsetattr(0, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    sys.exit(main())