"""Inline images, the photo gallery, and the popup viewer (kitty protocol)."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import webbrowser

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .. import config, kitty
from ..net import XhsClient
from .constants import ICON_COPY, ICON_IMAGE
from .widgets import open_image_with_system, system_viewer_notice

# Limit concurrent cover downloads so a grid page does not flood the network.
_THUMB_SEMAPHORE = asyncio.Semaphore(2)
_THUMB_BG = (28, 21, 25)  # canvas letterbox color (matches $surface)


def copy_image_bytes(data: bytes) -> tuple[bool, str]:
    """Put raw image bytes on the system clipboard (wl-copy/xclip/xsel/pngpaste).

    Returns (ok, message).
    """
    ext = ".png"
    mime = "image/png"
    try:
        from io import BytesIO

        from PIL import Image as PILImage

        im = PILImage.open(BytesIO(data))
        mime = {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "GIF": "image/gif",
            "WEBP": "image/webp",
            "BMP": "image/bmp",
        }.get(im.format, "application/octet-stream")
        ext = ".png" if im.format == "PNG" else f".{(im.format or 'bin').lower()}"
    except Exception:
        pass
    fd, path = tempfile.mkstemp(suffix=ext, prefix="xhs-copy-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        if shutil.which("wl-copy"):
            subprocess.run(["wl-copy", "--type", mime, path], check=True)
            return True, "图片已复制到剪切板"
        if shutil.which("xclip"):
            subprocess.run(["xclip", "-selection", "clipboard", "-t", mime, path], check=True)
            return True, "图片已复制到剪切板"
        if shutil.which("xsel"):
            subprocess.run(["xsel", "--clipboard", "--input", path], check=True)
            return True, "图片已复制到剪切板"
        if shutil.which("pngpaste"):
            subprocess.run(["pngpaste", path], check=True)
            return True, "图片已复制到剪切板"
    except Exception:
        pass
    return False, "未找到系统剪贴板工具"


class ViewerHint(Static, can_focus=True):
    pass


def _placeholder_text(img_id: int, cols: int, rows: int) -> Text:
    """Build a Rich renderable for a Kitty Unicode-placeholder grid."""
    text = Text()
    style = kitty.image_id_style(img_id)
    n = len(kitty.DIACRITICS)
    for y in range(rows):
        for x in range(cols):
            text.append(
                f"{kitty.PLACEHOLDER}{kitty.DIACRITICS[y % n]}{kitty.DIACRITICS[x % n]}",
                style=style,
            )
        if y + 1 < rows:
            text.append("\n")
    return text


def _thumb_text(img_id: int, cols: int, rows: int, label: str = "") -> Text:
    """Placeholder grid with an optional opaque label in the last row."""

    def cell(y: int, x: int) -> str:
        n = len(kitty.DIACRITICS)
        return f"{kitty.PLACEHOLDER}{kitty.DIACRITICS[y % n]}{kitty.DIACRITICS[x % n]}"

    style = kitty.image_id_style(img_id)
    label_cells = min(cols, cell_len(label)) if label else 0
    text = Text()
    for y in range(rows):
        usable = max(0, cols - label_cells) if y == rows - 1 else cols
        for x in range(usable):
            text.append(cell(y, x), style=style)
        if y == rows - 1 and label_cells:
            text.append(label, style="bold white on #000000")
        if y + 1 < rows:
            text.append("\n")
    return text


class ThumbImage(Static):
    """Fixed-aspect inline cover thumbnail (Kitty protocol, contain-fit).

    ``auto_width=True`` makes the thumbnail track the widget width (used by
    the grid cells); otherwise ``cols`` fixes the cell width and the height
    follows the requested aspect ratio.
    """

    def __init__(
        self,
        client: XhsClient,
        url: str,
        cols: int = 12,
        auto_width: bool = False,
        aspect: tuple[int, int] = (3, 4),
        label: str = "",
        **kwargs,
    ):
        super().__init__("", **kwargs)
        self.client = client
        self.url = url
        self.cols = max(1, cols)
        self.auto_width = auto_width
        self.aspect = aspect
        self.label = label
        self._img_id = (id(self) ^ 0x51D3A7) & 0xFFFFFF or 3
        self._loaded = False
        self._loading = False
        self._error = ""

    # -- geometry ---------------------------------------------------------

    def rows_for(self, cols: int) -> int:
        cw, ch = self.app.cell_size or (8, 16)
        rows = round(cols * cw * self.aspect[1] / max(1, self.aspect[0] * ch))
        return max(1, rows)

    def on_mount(self):
        if not kitty.enabled or not self.url:
            return
        if self.auto_width:
            if not self.size.width:
                return  # wait for on_resize
            self.cols = self.size.width
        else:
            self.styles.width = self.cols
        self.styles.height = self.rows_for(self.cols)
        self._schedule()

    def on_resize(self, event):
        if not kitty.enabled or not self.url:
            return
        cols = event.size.width if self.auto_width else self.cols
        if not cols or (cols == self.cols and self._loaded):
            return
        self.cols = cols
        self.styles.height = self.rows_for(cols)
        if self.is_mounted:
            self._schedule()

    # -- loading ----------------------------------------------------------

    def _schedule(self):
        if self._loading or not self.url or not kitty.enabled:
            return
        self._loading = True
        self.run_worker(self.load(), name=f"thumb-{self._img_id}")

    async def load(self):
        self._error = ""
        try:
            async with _THUMB_SEMAPHORE:
                data = await asyncio.to_thread(
                    self.client.fetch_image_bytes, self.url
                )
                if not self.is_mounted:
                    return
                from io import BytesIO
                from PIL import Image as PILImage

                source = PILImage.open(BytesIO(data)).convert("RGB")
                cw, ch = self.app.cell_size or (8, 16)
                cols = self.cols
                rows = self.rows_for(cols)
                box_w, box_h = cols * cw, rows * ch
                scale = min(box_w / source.width, box_h / source.height)
                width = max(1, round(source.width * scale))
                height = max(1, round(source.height * scale))
                resized = source.resize((width, height), PILImage.LANCZOS)
                canvas = PILImage.new("RGB", (box_w, box_h), _THUMB_BG)
                canvas.paste(
                    resized, ((box_w - width) // 2, (box_h - height) // 2)
                )
                driver = self.app._driver
                driver.write(
                    kitty.transmit_rgba(
                        canvas.convert("RGBA").tobytes(), self._img_id, box_w, box_h
                    )
                )
                driver.write(kitty.virtual_placement(self._img_id, cols, rows))
            self.update(_thumb_text(self._img_id, cols, rows, self.label))
            self._loaded = True
        except Exception as exc:
            self._error = str(exc)
            self.update(Text(""))
        finally:
            self._loading = False

    def set_label(self, label: str):
        self.label = label
        if self._loaded:
            self.update(_thumb_text(self._img_id, self.cols, self.rows_for(self.cols), label))

    def on_unmount(self):
        if self._loaded and kitty.enabled:
            try:
                self.app._driver.write(kitty.delete_image(self._img_id))
            except Exception:
                pass


class ImageViewerScreen(ModalScreen[None]):
    """Popup image viewer (Kitty protocol, unicode placeholders).

    + / - zoom, hjkl / arrows pan, comma / period switch image,
    o open in browser,
    Esc / q close.
    """

    BINDINGS = [
        Binding("escape,q", "close", "关闭"),
        Binding("+", "zoom_in", "放大"),
        Binding("=", "zoom_in", None, show=False),
        Binding("-", "zoom_out", "缩小"),
        Binding("h,left", "pan_left", None, show=False),
        Binding("j,down", "pan_down", None, show=False),
        Binding("k,up", "pan_up", None, show=False),
        Binding("l,right", "pan_right", None, show=False),
        Binding("full_stop", "next_img", "下一张"),
        Binding("comma", "prev_img", "上一张"),
        Binding("n", "next_img", None, show=False),
        Binding("p", "prev_img", None, show=False),
        Binding("o", "open_browser", "浏览器"),
    ]

    ZOOM_STEP = 1.5

    def __init__(
        self,
        client: XhsClient,
        urls: list[str],
        index: int = 0,
        caption: str = "",
    ):
        super().__init__()
        self.client = client
        self.urls = urls
        self.index = index
        self.caption = caption
        self._img = None  # PIL image
        self._img_id = (id(self) ^ 0x7F4A7C15) & 0xFFFFFF
        self._scale = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._ready = False
        self._failed = False

    def compose(self) -> ComposeResult:
        yield ViewerHint(
            "  + / - 缩放 · hjkl/方向键 移动 · ,/. 切换 · o 浏览器 · Esc 返回",
            id="iviewer-hint",
        )
        yield Vertical(
            Static("", id="iviewer-grid"),
            id="iviewer-stage",
        )
        yield Horizontal(
            Button(" 返回 ", id="iviewer-back"),
            Static("", id="iviewer-caption"),
            Static("倍率 1.00× · 位置 50%, 50%", id="iviewer-status"),
            id="iviewer-footer",
        )

    def on_mount(self):
        self.query_one("#iviewer-hint").focus()
        self.run_worker(self._load(), name="iviewer", group="iviewer", exclusive=True)

    async def _load(self):
        hint = self.query_one("#iviewer-hint")
        try:
            url = self.urls[self.index]
            hint.update(f"  加载图片 {self.index + 1}/{len(self.urls)} …")
            data = await asyncio.to_thread(self.client.fetch_image_bytes, url)
            from io import BytesIO
            from PIL import Image as PILImage

            im = PILImage.open(BytesIO(data))
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            self._img = im
            self._ready = True
            hint.update("")
            self._update_caption(len(data))
            self._fit()
        except Exception as e:
            self._failed = True
            hint.update(f"  图片加载失败：{e}  （o 在浏览器打开）")

    def _update_caption(self, byte_size: int = 0):
        if not self._img:
            return
        fmt = (self._img.format or self._img.mode or "图片").upper()
        iw, ih = self._img.size
        info = self.caption.strip() or f"{fmt} · {iw}×{ih} px · {byte_size / 1024:.1f} KiB"
        if len(self.urls) > 1:
            info = f"{self.index + 1}/{len(self.urls)} · {info}"
        self.query_one("#iviewer-caption", Static).update(Text(info, style="dim"))

    # -- geometry ---------------------------------------------------------

    def _viewport_px(self) -> tuple[int, int]:
        cw, ch = self.app.cell_size
        cols = max(20, self.app.size.width or 80)
        rows = max(10, self.app.size.height or 24)
        return max(1, (cols - 2) * cw), max(1, (rows - 5) * ch)

    def _fit(self):
        self._scale = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._emit()

    def _emit(self):
        if not self._ready or not self._img or not kitty.enabled:
            return
        cw, ch = self.app.cell_size
        vw, vh = self._viewport_px()
        iw, ih = self._img.size
        fit = min(vw / iw, vh / ih, 1.0)
        z = max(0.4, self._scale)
        # display size (px), capped to the viewport
        disp_w = max(1, min(vw, int(iw * fit * z)))
        disp_h = max(1, min(vh, int(ih * fit * z)))
        # cell grid first, then snap the transmitted pixels to the grid so
        # the image maps 1:1 onto the cells (no Ghostty-side rescale).
        cols = max(1, disp_w // max(1, cw))
        rows = max(1, disp_h // max(1, ch))
        disp_w = cols * cw
        disp_h = rows * ch
        # source window into the original image
        src_w = max(1.0, min(iw, disp_w / (fit * z)))
        src_h = max(1.0, min(ih, disp_h / (fit * z)))
        sx = min(max(0.0, iw - src_w), max(0.0, self._pan_x))
        sy = min(max(0.0, ih - src_h), max(0.0, self._pan_y))
        center_x = min(100.0, max(0.0, (sx + src_w / 2) / iw * 100))
        center_y = min(100.0, max(0.0, (sy + src_h / 2) / ih * 100))
        self.query_one("#iviewer-status", Static).update(
            f"倍率 {z:.2f}× · 位置 {center_x:.0f}%, {center_y:.0f}% "
        )
        try:
            from PIL import Image as PILImage

            crop = self._img.crop(
                (int(sx), int(sy), int(sx + src_w), int(sy + src_h))
            )
            resized = crop.resize((disp_w, disp_h), PILImage.LANCZOS)
            rgba = resized.convert("RGBA").tobytes()
            driver = self.app._driver
            driver.write(
                kitty.transmit_rgba(rgba, self._img_id, disp_w, disp_h)
            )
            # register the virtual placement grid (required by Ghostty/kitty)
            driver.write(kitty.virtual_placement(self._img_id, cols, rows))
            # placeholder grid: fg color = image id, diacritics = row/col
            grid = self.query_one("#iviewer-grid", Static)
            grid.update(_placeholder_text(self._img_id, cols, rows))
            grid.styles.width = cols
            grid.styles.height = rows
        except Exception:
            pass

    def _delete(self):
        if not kitty.enabled:
            return
        try:
            self.app._driver.write(kitty.delete_image(self._img_id))
        except Exception:
            pass

    def on_unmount(self):
        self._delete()

    # -- actions ----------------------------------------------------------

    def action_close(self):
        self._delete()
        self.dismiss(None)

    @on(Button.Pressed, "#iviewer-back")
    def _back_button(self, event: Button.Pressed):
        self.action_close()

    def action_zoom_in(self):
        if not self._ready:
            return
        self._scale = min(8.0, self._scale * self.ZOOM_STEP)
        self._pan_x *= self.ZOOM_STEP
        self._pan_y *= self.ZOOM_STEP
        self._emit()

    def action_zoom_out(self):
        if not self._ready:
            return
        self._scale = max(0.4, self._scale / self.ZOOM_STEP)
        self._pan_x /= self.ZOOM_STEP
        self._pan_y /= self.ZOOM_STEP
        self._emit()

    def _pan(self, dx: float, dy: float):
        if not self._ready:
            return
        cw, ch = self.app.cell_size
        self._pan_x += dx * cw
        self._pan_y += dy * ch
        self._emit()

    def action_pan_left(self):
        self._pan(-2, 0)

    def action_pan_right(self):
        self._pan(2, 0)

    def action_pan_up(self):
        self._pan(0, -2)

    def action_pan_down(self):
        self._pan(0, 2)

    def action_next_img(self):
        if len(self.urls) > 1:
            self.index = (self.index + 1) % len(self.urls)
            self._ready = False
            self.run_worker(
                self._load(), name="iviewer", group="iviewer", exclusive=True
            )

    def action_prev_img(self):
        if len(self.urls) > 1:
            self.index = (self.index - 1) % len(self.urls)
            self._ready = False
            self.run_worker(
                self._load(), name="iviewer", group="iviewer", exclusive=True
            )

    def action_open_browser(self):
        if self.urls:
            webbrowser.open(self.urls[self.index])


class GallerySection(Vertical):
    """Photo-first inline gallery: current image + prev/next controls.

    The first image is loaded inline automatically (when the terminal
    supports the kitty protocol); ``‹``/``›`` switch photos, ``放大`` opens
    the fullscreen viewer, and the copy button puts the photo on the
    system clipboard.
    """

    MAX_COLS = 80
    MAX_ROWS = 22

    def __init__(
        self,
        client: XhsClient,
        urls: list[str],
        caption: str = "",
        start: int = 0,
        title: str = "图集",
    ):
        super().__init__(classes="gallery")
        self.client = client
        self.urls = urls
        self.caption = caption
        self.title = title
        self.index = max(0, min(start, len(urls) - 1)) if urls else 0
        self._img_id = (id(self) ^ 0x2A9F31) & 0xFFFFFF or 2
        self._loaded = False
        self._loading = False
        self._img_data: bytes | None = None

    def compose(self) -> ComposeResult:
        self._prev = Button(" ‹ ", classes="gallery-prev")
        self._next = Button(" › ", classes="gallery-next")
        self._counter = Static("", classes="gallery-counter")
        self._zoom = Button(f"{ICON_IMAGE} 放大", classes="gallery-zoom")
        self._copy = Button(ICON_COPY, classes="gallery-copy")
        yield Horizontal(
            self._prev,
            self._counter,
            self._next,
            self._zoom,
            self._copy,
            classes="gallery-actions",
        )
        yield Static("", classes="img-inline")
        yield Static("", classes="img-caption")

    def on_mount(self):
        self._update_counter()
        if kitty.enabled and config.get("expand_images_by_default", True):
            self.run_worker(self._load_current(), name="gallery-load")

    def _update_counter(self):
        total = len(self.urls)
        self._counter.update(f" {self.index + 1}/{total} " if total else " 0/0 ")
        self._prev.disabled = total <= 1
        self._next.disabled = total <= 1
        if not kitty.enabled:
            self._zoom.label = " 用系统程序查看 "

    # -- loading ----------------------------------------------------------

    async def _load_current(self):
        if not self.urls:
            return
        grid = self.query_one(".img-inline", Static)
        self._loading = True
        self._zoom.label = f"{ICON_IMAGE} 加载中…"
        try:
            data = await asyncio.to_thread(
                self.client.fetch_image_bytes, self.urls[self.index]
            )
            self._img_data = data
            if not kitty.enabled:
                self._zoom.label = " 用系统程序查看 "
                return
            from io import BytesIO
            from PIL import Image as PILImage

            im = PILImage.open(BytesIO(data))
            fmt = (im.format or im.mode or "图片").upper()
            iw, ih = im.size
            cw, ch = self.app.cell_size
            available = self.size.width or self.app.size.width or 80
            max_cols = max(1, min(self.MAX_COLS, available - 2))
            scale = min(max_cols * cw / iw, self.MAX_ROWS * ch / ih, 1.0)
            cols = max(1, min(max_cols, int(iw * scale) // max(1, cw)))
            rows = max(1, min(self.MAX_ROWS, int(ih * scale) // max(1, ch)))
            disp_w, disp_h = cols * cw, rows * ch
            resized = im.convert("RGBA").resize((disp_w, disp_h), PILImage.LANCZOS)
            driver = self.app._driver
            driver.write(
                kitty.transmit_rgba(resized.tobytes(), self._img_id, disp_w, disp_h)
            )
            driver.write(kitty.virtual_placement(self._img_id, cols, rows))
            grid.update(_placeholder_text(self._img_id, cols, rows))
            grid.styles.width = cols
            grid.styles.height = rows
            info = (
                self.caption.strip()
                or f"{fmt} · {iw}×{ih} px · {len(data) / 1024:.1f} KiB"
            )
            prefix = f"{self.index + 1}/{len(self.urls)} · " if len(self.urls) > 1 else ""
            self.query_one(".img-caption", Static).update(
                Text(prefix + info, style="dim")
            )
            self._loaded = True
            self._zoom.label = f"{ICON_IMAGE} 放大"
        except Exception as e:
            grid.update(Text(f"图片加载失败：{e}", style="red"))
            self._zoom.label = f"{ICON_IMAGE} 重试"
        finally:
            self._loading = False

    def _render_current(self):
        if not kitty.enabled:
            self._update_counter()
            return
        if self._loaded:
            try:
                self.app._driver.write(kitty.delete_image(self._img_id))
            except Exception:
                pass
        self._loaded = False
        grid = self.query_one(".img-inline", Static)
        grid.update("")
        grid.styles.width = "auto"
        grid.styles.height = "auto"
        self.query_one(".img-caption", Static).update("")
        self.run_worker(self._load_current(), name="gallery-load")

    # -- public controls --------------------------------------------------

    def step(self, delta: int):
        if len(self.urls) <= 1:
            return
        self.index = (self.index + delta) % len(self.urls)
        self._update_counter()
        self._render_current()

    def open_viewer(self):
        if not self.urls:
            return
        if not kitty.enabled:
            self.app.run_worker(
                self._fallback_open(), name="img-sys", group="img", exclusive=True
            )
            return
        if not self._loaded:
            self.run_worker(self._load_current(), name="gallery-load")
            return
        self.app.push_screen(
            ImageViewerScreen(self.client, self.urls, self.index, self.caption)
        )

    def on_unmount(self):
        if self._loaded and kitty.enabled:
            try:
                self.app._driver.write(kitty.delete_image(self._img_id))
            except Exception:
                pass

    @on(Button.Pressed, ".gallery-prev")
    def _prev_pressed(self, event: Button.Pressed):
        self.step(-1)

    @on(Button.Pressed, ".gallery-next")
    def _next_pressed(self, event: Button.Pressed):
        self.step(1)

    @on(Button.Pressed, ".gallery-zoom")
    def _zoom_pressed(self, event: Button.Pressed):
        self.open_viewer()

    @on(Button.Pressed, ".gallery-copy")
    def _copy_pressed(self, event: Button.Pressed):
        url = self.urls[self.index] if self.urls else ""
        data = self._img_data
        if not data:
            if url:
                self.app.copy_to_clipboard(url)
                self.app.notify("已复制图片链接", timeout=2)
            return
        ok, msg = copy_image_bytes(data)
        if ok:
            self.app.notify(msg, timeout=3)
        else:
            self.app.copy_to_clipboard(url)
            self.app.notify("未找到系统剪贴板工具，已复制图片链接", timeout=3)

    async def _fallback_open(self):
        try:
            ok = await asyncio.to_thread(
                open_image_with_system, self.client, self.urls[self.index]
            )
            if ok:
                self.app.notify(system_viewer_notice(), timeout=4)
        except Exception as e:
            self.app.notify(f"图片打开失败：{e}", severity="error", timeout=6)


class ImageSection(Vertical):
    """Bounded inline image with a button opening the full viewer."""

    MAX_COLS = 64
    MAX_ROWS = 18

    def __init__(self, client: XhsClient, urls: list[str], no: int, caption: str = ""):
        super().__init__(classes="imgsec")
        self.client = client
        self.urls = urls
        self.no = no
        self.caption = caption
        self.viewer_urls = list(urls)
        self.viewer_index = 0
        self._img_id = (id(self) ^ 0x35A71E) & 0xFFFFFF or 1
        self._loaded = False
        self._loading = False
        self._img_data: bytes | None = None

    def compose(self) -> ComposeResult:
        self._btn = Button(f"{ICON_IMAGE} 图片 {self.no}", classes="imgbtn")
        self._collapse_btn = Button("折叠图片", classes="img-collapse")
        self._collapse_btn.display = False
        self._copy_btn = Button(ICON_COPY, classes="img-copy")
        self._copy_btn.display = False
        yield Horizontal(
            self._collapse_btn,
            self._btn,
            self._copy_btn,
            classes="img-actions",
        )
        yield Static("", classes="img-inline")
        yield Static("", classes="img-caption")

    async def _load_inline(self):
        grid = self.query_one(".img-inline", Static)
        self._loading = True
        self._btn.label = f"{ICON_IMAGE} 加载中…"
        try:
            data = await asyncio.to_thread(self.client.fetch_image_bytes, self.urls[0])
            self._img_data = data
            from io import BytesIO
            from PIL import Image as PILImage

            im = PILImage.open(BytesIO(data))
            fmt = (im.format or im.mode or "图片").upper()
            iw, ih = im.size
            cw, ch = self.app.cell_size
            available = self.size.width or self.app.size.width or 80
            max_cols = max(1, min(self.MAX_COLS, available - 2))
            scale = min(max_cols * cw / iw, self.MAX_ROWS * ch / ih, 1.0)
            cols = max(1, min(max_cols, int(iw * scale) // max(1, cw)))
            rows = max(1, min(self.MAX_ROWS, int(ih * scale) // max(1, ch)))
            disp_w, disp_h = cols * cw, rows * ch
            resized = im.convert("RGBA").resize((disp_w, disp_h), PILImage.LANCZOS)
            driver = self.app._driver
            driver.write(kitty.transmit_rgba(resized.tobytes(), self._img_id, disp_w, disp_h))
            driver.write(kitty.virtual_placement(self._img_id, cols, rows))
            grid.update(_placeholder_text(self._img_id, cols, rows))
            grid.styles.width = cols
            grid.styles.height = rows
            info = self.caption.strip() or f"{fmt} · {iw}×{ih} px · {len(data) / 1024:.1f} KiB"
            self.query_one(".img-caption", Static).update(Text(info, style="dim"))
            self._loaded = True
            self._btn.label = f"{ICON_IMAGE} 放大"
            self._collapse_btn.display = True
            self._copy_btn.display = True
        except Exception as e:
            grid.update(Text(f"图片加载失败：{e}", style="red"))
            self._btn.label = f"{ICON_IMAGE} 图片 {self.no}（重试）"
        finally:
            self._loading = False

    def on_unmount(self):
        if self._loaded and kitty.enabled:
            try:
                self.app._driver.write(kitty.delete_image(self._img_id))
            except Exception:
                pass

    @on(Button.Pressed)
    def _on_pressed(self, event: Button.Pressed):
        if event.button is self._copy_btn:
            self._copy_image()
            return
        if event.button is self._collapse_btn:
            self._collapse_inline()
            return
        if event.button is not self._btn:
            return
        if not kitty.enabled:
            self.app.run_worker(
                self._fallback_open(), name="img-sys", group="img", exclusive=True
            )
            return
        if self._loading:
            return
        if not self._loaded:
            self.run_worker(self._load_inline(), name=f"inline-img-{self.no}")
            return
        self.app.push_screen(
            ImageViewerScreen(
                self.client, self.viewer_urls, self.viewer_index, self.caption
            )
        )

    def _collapse_inline(self):
        if not self._loaded:
            return
        try:
            self.app._driver.write(kitty.delete_image(self._img_id))
        except Exception:
            pass
        grid = self.query_one(".img-inline", Static)
        grid.update("")
        grid.styles.width = "auto"
        grid.styles.height = "auto"
        self.query_one(".img-caption", Static).update("")
        self._loaded = False
        self._img_data = None
        self._btn.label = f"{ICON_IMAGE} 图片 {self.no}"
        self._collapse_btn.display = False
        self._copy_btn.display = False

    def _copy_image(self):
        url = self.urls[0]
        data = getattr(self, "_img_data", None)
        if not data:
            self.app.copy_to_clipboard(url)
            self.app.notify("已复制图片链接", timeout=2)
            return
        ok, msg = copy_image_bytes(data)
        if ok:
            self.app.notify(msg, timeout=3)
        else:
            self.app.copy_to_clipboard(url)
            self.app.notify("未找到系统剪贴板工具，已复制图片链接", timeout=3)

    async def _fallback_open(self):
        try:
            ok = await asyncio.to_thread(open_image_with_system, self.client, self.urls[0])
            if ok:
                self.app.notify(system_viewer_notice(), timeout=4)
        except Exception as e:
            self.app.notify(f"图片打开失败：{e}", severity="error", timeout=6)
