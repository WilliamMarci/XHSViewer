"""Modal dialogs: search prompt, table viewer, url prompt."""
from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class SearchPromptScreen(ModalScreen[str]):
    """Modal input: search answers/questions (Ctrl+S)."""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(" 搜索知乎：输入关键词后按 Enter", classes="cmt-title"),
            Input(placeholder="搜索问题 / 回答 / 文章…", id="search-input"),
            Horizontal(
                Button(" 搜索 ", id="search-go", classes="modebtn"),
                Button(" 取消 ", id="search-cancel", classes="modebtn"),
                classes="row",
            ),
            id="url-panel",
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
    """Modal input: paste a zhihu link to open it (Ctrl+O)."""

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(" 打开链接：粘贴知乎链接后按 Enter 或点「打开」", classes="cmt-title"),
            Input(
                placeholder="https://www.zhihu.com/question/123/answer/456",
                id="url-input",
            ),
            Horizontal(
                Button(" 打开 ", id="url-go", classes="modebtn"),
                Button(" 取消 ", id="url-cancel", classes="modebtn"),
                classes="row",
            ),
            id="url-panel",
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