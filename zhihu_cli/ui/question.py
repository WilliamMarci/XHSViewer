"""Question detail screen: paginated answer list with page search."""
from __future__ import annotations

import asyncio
import webbrowser

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from .. import history
from ..net import FeedItem, NotLoggedIn, ZhihuSession
from .widgets import (
    AnswerHeader,
    AnswerTopBar,
    BottomBar,
    Card,
    FeedScroll,
    PageSearchBar,
    _close_page_search,
    _find_in_widgets,
    _open_page_search,
)


class QuestionScreen(Screen):
    """Question detail with a paginated, scrollable list of answer cards."""

    BINDINGS = [
        Binding("escape,q", "back", "返回", show=False),
        Binding("j,down", "next_card", "", show=False),
        Binding("k,up", "prev_card", "", show=False),
        Binding("h,left", "search_prev", "", show=False),
        Binding("l,right", "search_next", "", show=False),
        Binding("enter", "open_card", "进入回答"),
        Binding("o", "open_browser", "浏览器"),
        Binding("/", "page_search", "", show=False),
    ]

    def __init__(self, client: ZhihuSession, item: FeedItem):
        super().__init__()
        self.client = client
        self.item = item
        self._answer_offset = 0
        self.is_end = False
        self._loading = False
        self.focus_index = 0
        self._detail_open = False

    def compose(self) -> ComposeResult:
        yield AnswerTopBar("提问", detail_toggle=True)
        yield AnswerHeader(id="answer-header")
        yield FeedScroll(id="question-answers")
        yield PageSearchBar(id="page-search")
        yield BottomBar(id="bottombar")

    def on_mount(self):
        self.query_one("#answer-header", AnswerHeader).set_item(self.item)
        try:
            history.record(self.item)
        except Exception:
            pass
        self.run_worker(self._load_more(), name="question-answers", exclusive=True)

    async def _load_more(self):
        if self._loading or self.is_end:
            return
        self._loading = True
        self.query_one("#bottombar", BottomBar).busy("加载回答…")
        try:
            items, is_end = await asyncio.to_thread(
                self.client.question_answers, self.item.qid, self._answer_offset
            )
            box = self.query_one("#question-answers")
            cards = [Card(answer, show_title=False) for answer in items]
            if cards:
                box.mount(*cards)
                self._answer_offset += len(items)
                if len(self.query(Card)) == len(cards):
                    cards[0].focus()
            elif self._answer_offset == 0:
                box.mount(Static("（该问题暂无回答）", classes="hint"))
            self.is_end = is_end or not items
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=8)
        except Exception as e:
            self.notify(f"回答加载失败：{e}", severity="error", timeout=8)
        finally:
            self._loading = False
            self.query_one("#bottombar", BottomBar).idle()

    def _detail_renderable(self) -> Text:
        t = Text()
        title = " ".join((self.item.title or "").split())
        if title:
            t.append(title, style="bold")
            t.append("\n")
        excerpt = " ".join((self.item.excerpt or "").split())
        if excerpt:
            t.append(excerpt, style="#94a3b8")
        return t

    @on(Button.Pressed, "#question-detail-toggle")
    def _toggle_question_detail(self, event: Button.Pressed):
        box = self.query_one("#question-answers")
        toggle = self.query_one("#question-detail-toggle", Button)
        if self._detail_open:
            detail = box.query("#question-detail")
            if detail:
                detail.first().remove()
            self._detail_open = False
            toggle.label = " ▼ "
            return
        widget = Static(
            self._detail_renderable(), id="question-detail", classes="question-detail"
        )
        first = box.children[0] if box.children else None
        if first is not None:
            box.mount(widget, before=first)
        else:
            box.mount(widget)
        self._detail_open = True
        toggle.label = " ▲ "
        box.scroll_home(animate=False, immediate=True)

    def check_load_more(self):
        if self._loading or self.is_end:
            return
        box = self.query_one("#question-answers")
        if box.max_scroll_y is not None and box.scroll_y >= box.max_scroll_y - 8:
            self.run_worker(self._load_more(), name="question-more", exclusive=True)

    def _cards(self) -> list[Card]:
        return list(self.query(Card))

    def _focus(self, index: int):
        cards = self._cards()
        if not cards:
            return
        self.focus_index = max(0, min(index, len(cards) - 1))
        card = cards[self.focus_index]
        card.focus()
        self.query_one("#question-answers").scroll_to_widget(
            card, animate=False, immediate=True
        )

    def action_next_card(self):
        self._focus(self.focus_index + 1)

    def action_prev_card(self):
        self._focus(self.focus_index - 1)

    def action_open_card(self):
        cards = self._cards()
        if cards:
            from .content import ContentScreen

            self.app.push_screen(ContentScreen(self.client, cards[self.focus_index].item))

    def action_open_browser(self):
        webbrowser.open(self.item.url)

    def action_back(self):
        if _close_page_search(self):
            return
        self.app.pop_screen()

    def action_page_search(self):
        _open_page_search(self)

    def _page_find(self, direction: int):
        box = self.query_one("#question-answers")
        _find_in_widgets(self, list(box.query(Card)), box, direction)

    def action_search_prev(self):
        if self.query_one(PageSearchBar).display:
            self._page_find(-1)

    def action_search_next(self):
        if self.query_one(PageSearchBar).display:
            self._page_find(1)

    @on(Button.Pressed, "#answer-back")
    def _question_back_button(self, event: Button.Pressed):
        self.action_back()

    @on(Input.Submitted, "#page-search-input")
    def _question_search_submit(self, event: Input.Submitted):
        self._page_find(1)

    @on(Button.Pressed, ".page-search-up")
    def _question_search_up(self, event: Button.Pressed):
        self._page_find(-1)

    @on(Button.Pressed, ".page-search-down")
    def _question_search_down(self, event: Button.Pressed):
        self._page_find(1)

    @on(Button.Pressed, ".page-search-close")
    def _question_search_close(self, event: Button.Pressed):
        _close_page_search(self)