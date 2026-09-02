"""Comments popup."""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..net import FeedItem, NotLoggedIn, ZhihuSession, fmt_num, fmt_time
from .constants import ICON_COMMENT
from .widgets import CommentScroll, StatusLine


class CommentsScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,q", "close", "关闭"),
        Binding("j,down", "scroll_down", "", show=False),
        Binding("k,up", "scroll_up", "", show=False),
        Binding("space", "page_down", "", show=False),
    ]

    def __init__(self, client: ZhihuSession, item: FeedItem):
        super().__init__()
        self.client = client
        self.item = item
        self._offset = 0
        self.is_end = False
        self._loading = False

    def compose(self) -> ComposeResult:
        count = fmt_num(self.item.comments) if self.item.comments else ""
        yield Vertical(
            Horizontal(
                Static(
                    f" {ICON_COMMENT} · {self.item.author or '匿名用户'}{('（' + count + '）') if count else ''}",
                    classes="cmt-head",
                ),
                Static("", classes="cmt-spacer"),
                Button(" 返回 ", id="comments-close", classes="cmt-close"),
                id="cmt-title-row",
            ),
            CommentScroll(id="comments"),
            StatusLine(id="status"),
            id="cmt-panel",
        )

    @on(Button.Pressed, "#comments-close")
    def _comments_close(self, event: Button.Pressed):
        self.action_close()

    def on_mount(self):
        self.run_worker(self._load(), name="cmt", group="cmt", exclusive=True)

    def _busy(self, text: str = "加载中…"):
        self.query_one("#status", StatusLine).busy(text)

    def _idle(self):
        self.query_one("#status", StatusLine).idle()

    async def _load(self):
        if self._loading or self.is_end:
            return
        self._loading = True
        self._busy("加载评论…")
        try:
            comments, is_end = await asyncio.to_thread(
                self.client.answer_comments, self.item.aid, self._offset
            )
            box = self.query_one("#comments")
            if self._offset == 0:
                box.remove_children()
            if not comments and self._offset == 0:
                box.mount(Static(" （暂无评论）", classes="hint"))
            for c in comments:
                box.mount(self._comment_static(c))
            self._offset += len(comments)
            self.is_end = is_end
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=8)
            self.is_end = True
        except Exception as e:
            self.notify(f"评论加载失败：{e}", severity="error", timeout=8)
        finally:
            self._loading = False
            self._idle()

    def check_more(self):
        if self._loading or self.is_end:
            return
        box = self.query_one("#comments")
        if box.max_scroll_y is not None and box.scroll_y >= box.max_scroll_y - 8:
            self.run_worker(
                self._load(), name="cmt-more", group="cmt", exclusive=True
            )

    @staticmethod
    def _comment_static(c) -> Static:
        t = Text()
        t.append(f"{c.author or '匿名'}", style="bold #eab308")
        if c.reply_to:
            t.append(f" → {c.reply_to}", style="#8a93a3")
        if c.votes:
            t.append(f"  ▲ {fmt_num(c.votes)}", style="#34d399")
        if c.created:
            t.append(f"  {fmt_time(c.created)}", style="#8a93a3")
        t.append("\n")
        t.append(c.content, style="#c8d0dc")
        return Static(t, classes="comment")

    def action_close(self):
        self.dismiss(None)

    def action_scroll_down(self):
        self.query_one("#comments").scroll_down(animate=False, immediate=True)

    def action_scroll_up(self):
        self.query_one("#comments").scroll_up(animate=False, immediate=True)

    def action_page_down(self):
        box = self.query_one("#comments")
        box.scroll_to(
            y=box.scroll_y + box.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )