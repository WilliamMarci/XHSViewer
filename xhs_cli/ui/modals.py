"""Modal dialogs: search prompt, table viewer, url prompt, captcha verify."""
from __future__ import annotations

import webbrowser

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from .constants import ICON_CANCEL, ICON_OPEN

VERIFY_URL = "https://www.xiaohongshu.com/explore"


class VerificationRequiredScreen(ModalScreen[None]):
    """Captcha / 风控 popup with a one-line button that opens the browser."""

    BINDINGS = [
        Binding("escape,q", "close", "关闭"),
        Binding("o", "open_browser", "浏览器验证"),
    ]

    def __init__(self, message: str, url: str = VERIFY_URL):
        super().__init__()
        self.message = message
        self.url = url

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(" 需要完成小红书安全验证 ", id="verify-title"),
            Static(self.message, id="verify-message"),
            Horizontal(
                Button(
                    " 在浏览器中验证 ",
                    id="verify-open",
                    classes="verify-btn verify-open",
                ),
                Button(" 关闭 ", id="verify-close", classes="verify-btn"),
                id="verify-actions",
            ),
            id="verify-panel",
        )

    @on(Button.Pressed, "#verify-open")
    def _verify_open(self, event: Button.Pressed):
        webbrowser.open(self.url)

    @on(Button.Pressed, "#verify-close")
    def _verify_close(self, event: Button.Pressed):
        self.dismiss(None)

    def action_open_browser(self):
        webbrowser.open(self.url)

    def action_close(self):
        self.dismiss(None)


class SearchPromptScreen(ModalScreen[str]):
    """Modal input: search notes (Ctrl+S)."""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Input(
                placeholder="搜索笔记 / 话题…",
                id="search-input",
                classes="prompt-input",
            ),
            Button(ICON_OPEN, id="search-go", classes="prompt-btn prompt-go"),
            Button(ICON_CANCEL, id="search-cancel", classes="prompt-btn prompt-cancel"),
            id="prompt-panel",
        )

    def on_mount(self):
        try:
            self.query_one("#search-input", Input).focus()
        except Exception:
            pass

    @on(Input.Submitted, "#search-input")
    def _submitted(self, event: Input.Submitted):
        self._submit()

    @on(Button.Pressed, "#search-go")
    def _go(self, event: Button.Pressed):
        self._submit()

    @on(Button.Pressed, "#search-cancel")
    def _cancel(self, event: Button.Pressed):
        self.dismiss(None)

    def _submit(self):
        q = self.query_one("#search-input", Input).value.strip()
        self.dismiss(q or None)

    def action_cancel(self):
        self.dismiss(None)


class TableScreen(ModalScreen[None]):
    """Full-table popup for tables too wide to show inline.

    hjkl / arrows pan both axes; space pages down; Esc/q closes.
    """

    BINDINGS = [
        Binding("escape,q", "close", "关闭"),
        Binding("h,left", "left", "", show=False),
        Binding("j,down", "down", "", show=False),
        Binding("k,up", "up", "", show=False),
        Binding("l,right", "right", "", show=False),
        Binding("space", "page_down", "", show=False),
        Binding("g", "top", "", show=False),
        Binding("G", "bottom", "", show=False),
    ]

    def __init__(self, text_obj: Text, title: str = "表格"):
        super().__init__()
        self._table = text_obj
        self._title = title

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f" {self._title}  （hjkl/方向键 移动 · 空格 翻页 · Esc 关闭）", classes="cmt-title"),
            ScrollableContainer(Static(self._table, id="table-body"), id="table-scroll"),
            id="table-panel",
        )

    def on_mount(self):
        lines = (self._table.plain or "").split("\n")
        w = max((cell_len(ln) for ln in lines), default=10) + 2
        body = self.query_one("#table-body", Static)
        body.styles.width = w
        box = self.query_one("#table-scroll", ScrollableContainer)
        box.focus()

    def _box(self) -> ScrollableContainer:
        return self.query_one("#table-scroll", ScrollableContainer)

    def action_left(self):
        self._box().scroll_to(x=self._box().scroll_x - 2, animate=False, immediate=True)

    def action_right(self):
        self._box().scroll_to(x=self._box().scroll_x + 2, animate=False, immediate=True)

    def action_up(self):
        self._box().scroll_to(y=self._box().scroll_y - 1, animate=False, immediate=True)

    def action_down(self):
        self._box().scroll_to(y=self._box().scroll_y + 1, animate=False, immediate=True)

    def action_page_down(self):
        box = self._box()
        box.scroll_to(
            y=box.scroll_y + box.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )

    def action_top(self):
        self._box().scroll_home(animate=False, immediate=True)

    def action_bottom(self):
        self._box().scroll_end(animate=False, immediate=True)

    def action_close(self):
        self.dismiss(None)


class UrlPromptScreen(ModalScreen[str]):
    """Modal input: paste a Xiaohongshu link to open it (Ctrl+O)."""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Input(
                placeholder="https://www.xiaohongshu.com/explore/…",
                id="url-input",
                classes="prompt-input",
            ),
            Button(ICON_OPEN, id="url-go", classes="prompt-btn prompt-go"),
            Button(ICON_CANCEL, id="url-cancel", classes="prompt-btn prompt-cancel"),
            id="prompt-panel",
        )

    def on_mount(self):
        try:
            self.query_one("#url-input", Input).focus()
        except Exception:
            pass

    @on(Input.Submitted, "#url-input")
    def _submitted(self, event: Input.Submitted):
        self._submit()

    @on(Button.Pressed, "#url-go")
    def _go(self, event: Button.Pressed):
        self._submit()

    @on(Button.Pressed, "#url-cancel")
    def _cancel(self, event: Button.Pressed):
        self.dismiss(None)

    def _submit(self):
        url = self.query_one("#url-input", Input).value.strip()
        self.dismiss(url or None)

    def action_cancel(self):
        self.dismiss(None)
