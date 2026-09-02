"""Inline images and the popup viewer (kitty graphics protocol)."""
from __future__ import annotations

import asyncio
import webbrowser

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .. import kitty
from ..net import ZhihuSession
from .constants import ICON_IMAGE
from .widgets import open_image_with_system, system_viewer_notice


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
        client: ZhihuSession,
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


class ImageSection(Vertical):
    """Bounded inline image with a button opening the full viewer."""

    MAX_COLS = 64
    MAX_ROWS = 18

    def __init__(self, client: ZhihuSession, urls: list[str], no: int, caption: str = ""):
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

    def compose(self) -> ComposeResult:
        self._btn = Button(f"{ICON_IMAGE} 图片 {self.no}", classes="imgbtn")
        self._collapse_btn = Button("折叠图片", classes="img-collapse")
        self._collapse_btn.display = False
        yield Horizontal(self._collapse_btn, self._btn, classes="img-actions")
        yield Static("", classes="img-inline")
        yield Static("", classes="img-caption")

    async def _load_inline(self):
        grid = self.query_one(".img-inline", Static)
        self._loading = True
        self._btn.label = f"{ICON_IMAGE} 加载中…"
        try:
            data = await asyncio.to_thread(self.client.fetch_image_bytes, self.urls[0])
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
        self._btn.label = f"{ICON_IMAGE} 图片 {self.no}"
        self._collapse_btn.display = False

    async def _fallback_open(self):
        try:
            ok = await asyncio.to_thread(open_image_with_system, self.client, self.urls[0])
            if ok:
                self.app.notify(system_viewer_notice(), timeout=4)
        except Exception as e:
            self.app.notify(f"图片打开失败：{e}", severity="error", timeout=6)