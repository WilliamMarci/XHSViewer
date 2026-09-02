#!/usr/bin/env python3
"""独立图片显示演示（Kitty 协议 unicode placeholder，支持 Ghostty/kitty/tmux）。

仅负责在终端中显示图片，用于确认图片可以正常打开。
实现方式与 yazi/ratatui 一致：U=1 传输 + U+10EEEE 占位符网格。

用法：
    python demo_image.py [图片文件或URL]        # 默认生成一张测试图

按键：
    + / -      放大 / 缩小
    h j k l    平移（或方向键）
    o          在浏览器中打开（URL 时）
    q / Esc    退出
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import termios
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
    arg = next((a for a in sys.argv[1:] if a != "--diag"), None)

    if not kitty.terminal_supports_kitty() and not os.environ.get("ZHIHU_KITTY"):
        print("提示：未检测到 Kitty 兼容终端（如 Ghostty）。", file=sys.stderr)
        print("若你确信终端支持，可设置 ZHIHU_KITTY=1 强制启用。", file=sys.stderr)
        return 1

    cell = kitty.query_cell_size()
    if cell is None:
        cell = (8, 16)
    cw, ch = cell
    print(f"终端单元尺寸: {cw}x{ch}px", file=sys.stderr)

    data, desc = load_image(arg)
    from PIL import Image as PILImage

    im = PILImage.open(io.BytesIO(data))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    print(f"图片: {im.size[0]}x{im.size[1]}px  ({desc})", file=sys.stderr)

    img_id = (os.getpid() & 0xFFFF) ^ 0x5A5A
    diag = "--diag" in sys.argv
    scale = 1.0
    pan_x = 0.0
    pan_y = 0.0

    old = termios.tcgetattr(0)
    try:
        tty.setraw(0)

        def write(s: str) -> None:
            os.write(1, s.encode("utf-8", "ignore"))

        def viewport() -> tuple[int, int]:
            cols, rows = shutil.get_terminal_size()
            return max(1, (cols - 2) * cw), max(1, (rows - 4) * ch)

        status: list[str] = []

        def read_response() -> str:
            import select

            r, _, _ = select.select([0], [], [], 0.8)
            if not r:
                return ""
            try:
                data = os.read(0, 65536).decode("utf-8", "ignore")
            except OSError:
                return ""
            for m in re.finditer(r"\x1b_G([^\x1b]*)\x1b\\", data):
                return m.group(1)
            return data.strip()[:80]

        def emit() -> None:
            vw, vh = viewport()
            iw, ih = im.size
            fit = min(vw / iw, vh / ih, 1.0)
            z = max(0.4, scale)
            disp_w = max(1, min(vw, int(iw * fit * z)))
            disp_h = max(1, min(vh, int(ih * fit * z)))
            src_w = max(1.0, min(iw, disp_w / (fit * z)))
            src_h = max(1.0, min(ih, disp_h / (fit * z)))
            sx = min(max(0.0, iw - src_w), max(0.0, pan_x))
            sy = min(max(0.0, ih - src_h), max(0.0, pan_y))
            cols = max(1, disp_w // cw)
            rows = max(1, disp_h // ch)
            crop = im.crop((int(sx), int(sy), int(sx + src_w), int(sy + src_h)))
            resized = crop.resize((disp_w, disp_h), PILImage.LANCZOS)
            rgba = resized.convert("RGBA").tobytes()
            if diag:
                # q=1 so Ghostty responds on success AND failure
                seq = kitty.transmit_rgba(rgba, img_id, disp_w, disp_h)
                seq = seq.replace("q=2,a=T", "q=1,a=T", 1)
                write(seq)
                resp = read_response()
                status.append(f"传输响应: {resp}" if resp else "传输响应: (无)")
            else:
                write(kitty.transmit_rgba(rgba, img_id, disp_w, disp_h))
            tcols, trows = shutil.get_terminal_size()
            row = max(1, (trows - rows) // 2)
            col = max(0, (tcols - cols) // 2)
            write(f"\x1b[{row + 1};{col + 1}H" + kitty.placeholder_cells(img_id, cols, rows))

        def cleanup() -> None:
            try:
                write(kitty.delete_image(img_id))
            except Exception:
                pass

        write("\x1b[2J\x1b[H")
        emit()
        hint = f" 图片演示: 缩放 1.00x  ({desc})  + / - 缩放  hjkl 平移  q 退出"
        if status:
            hint += "  " + status[0]
        print("\x1b[1;1H\x1b[K" + hint, end="", flush=True)

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
            hint = f" 图片演示: 缩放 {scale:.2f}x  ({desc})  q 退出"
            if status:
                hint += "  " + status[0]
            print("\x1b[1;1H\x1b[K" + hint, end="", flush=True)
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