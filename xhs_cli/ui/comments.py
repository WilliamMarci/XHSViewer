"""Comments popup (XHS comments with replies, cursor pagination)."""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..net import Comment, FeedItem, NotLoggedIn, VerificationRequired, XhsClient, fmt_num, fmt_time
from .constants import ICON_COMMENT, ICON_LIKE
from .widgets import CommentScroll, StatusLine


class CommentsScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,q", "close", "关闭"),
        Binding("j,down", "scroll_down", "", show=False),
        Binding("k,up", "scroll_up", "", show=False),
    ]

    def __init__(self, client: XhsClient, item: FeedItem):
        super().__init__()
        self.client = client
        self.item = item
        self._cursor = ""
        self.is_end = False
        self._loading = False

    def compose(self) -> ComposeResult:
        count = fmt_num(self.item.comments) if self.item.comments else ""
        yield Vertical(
            Horizontal(
                Static(
                    f" {ICON_COMMENT} · {self.item.title or self.item.author or '笔记'}"
                    f"{('（' + count + '）') if count else ''}",
                    classes="cmt-head",
                ),
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
        try:
            self.query_one("#status", StatusLine).busy(text)
        except Exception:
            pass

    def _idle(self):
        try:
            self.query_one("#status", StatusLine).idle()
        except Exception:
            pass

    async def _load(self):
        if self._loading or self.is_end:
            return
        self._loading = True
        self._busy("加载评论…")
        try:
            comments, cursor, has_more = await asyncio.to_thread(
                self.client.note_comments,
                self.item.rid,
                self._cursor,
                self.item.xsec_token,
            )
            box = self.query_one("#comments")
            if self._cursor == "":
                box.remove_children()
            if not comments and self._cursor == "":
                box.mount(Static(" （暂无评论）", classes="hint"))
            for comment in comments:
                box.mount(self._comment_static(comment))
                for reply in comment.replies:
                    box.mount(self._comment_static(reply, reply=True))
            self._cursor = cursor
            self.is_end = has_more is False or (not comments and not cursor)
        except VerificationRequired as e:
            self.app.show_verification_required(str(e))
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
    def _comment_static(comment: Comment, reply: bool = False) -> Static:
        t = Text()
        if reply:
            t.append("  ↳ ", style="#8a7580")
        t.append(f"{comment.author or '未知用户'}", style="bold #ffb3c1")
        if comment.votes:
            t.append(f"  {ICON_LIKE} {fmt_num(comment.votes)}", style="#ff4d6d")
        if comment.created:
            t.append(f"  {fmt_time(comment.created)}", style="#b9a3aa")
        if comment.ip:
            t.append(f"  IP {comment.ip}", style="#b9a3aa")
        if reply:
            t.append("  作者/回复", style="#8a7580")
        t.append("\n")
        t.append(comment.content, style="#e8dde1")
        return Static(t, classes="comment comment-reply" if reply else "comment")

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
