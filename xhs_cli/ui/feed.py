"""Card-list screens: the feed (推荐/热门/历史 tabs) and search results."""
from __future__ import annotations

import asyncio
import webbrowser

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Static

from .. import config, history, kitty
from ..net import NotLoggedIn, VerificationRequired, XhsClient
from .widgets import (
    BottomBar,
    Card,
    FeedScroll,
    FeedTopBar,
    GridCard,
    HistoryEndRule,
    HistoryEntry,
    HistoryNoteRow,
    NoteTopBar,
    build_card,
    grid_column_count,
    push_item_screen,
)


def _initial_view() -> str:
    if not kitty.enabled:
        return "list"
    view = str(config.get("feed_view", "list") or "list")
    return view if view in ("list", "grid") else "list"


def _initial_show_images() -> bool:
    return bool(config.get("feed_show_images", False)) and kitty.enabled


class FeedScreen(Screen):
    BINDINGS = [
        Binding("comma,less_than_sign", "prev_feed_tab", "", show=False),
        Binding("full_stop,greater_than_sign", "next_feed_tab", "", show=False),
        Binding("j,down", "next_card", "下一条", show=False),
        Binding("k,up", "prev_card", "上一条", show=False),
        Binding("enter", "open_card", "展开"),
        Binding("space", "open_card", "", show=False),
        Binding("r", "refresh", "刷新"),
        Binding("o", "open_browser", "浏览器"),
        Binding("g", "go_top", "顶部", show=False),
        Binding("G", "go_bottom", "底部", show=False),
        Binding("q,ctrl+c", "quit_app", "退出"),
    ]

    def __init__(self, client: XhsClient, user: dict):
        super().__init__()
        self.client = client
        self.user = user or {}
        self.is_end = False
        self._loading = False
        self.focus_index = 0
        self._mode = "recommend"  # recommend | hot | history
        self._mode_cache: dict[str, dict] = {}
        self._view = _initial_view()
        self._show_images = _initial_show_images()
        self._resetting = False

    def compose(self) -> ComposeResult:
        yield FeedTopBar(self.user, self._mode)
        yield FeedScroll(id="feed")
        yield BottomBar(feed_brand=True, id="bottombar")

    def on_mount(self):
        self.run_worker(
            self._initial_load(), name="feed-initial", group="feed", exclusive=True
        )

    @on(Button.Pressed, "#tab-rec")
    def _tab_rec(self, event: Button.Pressed):
        self._set_feed_mode("recommend")

    @on(Button.Pressed, "#tab-hot")
    def _tab_hot(self, event: Button.Pressed):
        self._set_feed_mode("hot")

    @on(Button.Pressed, "#tab-history")
    def _tab_history(self, event: Button.Pressed):
        self._set_feed_mode("history")

    def _set_feed_mode(self, mode: str):
        if self._mode == mode:
            return
        if self._mode in ("recommend", "hot"):
            self._cache_current()
        self._mode = mode
        self.query_one(FeedTopBar).set_mode(self._mode)
        if mode == "recommend" and config.get("auto_refresh_recommend", False):
            self._mode_cache.pop("recommend", None)
        self.run_worker(
            self._reset_feed(use_cache=True),
            name="feed-switch", group="feed", exclusive=True,
        )

    def _cycle_feed_tab(self, direction: int):
        modes = ("recommend", "hot", "history")
        index = modes.index(self._mode)
        self._set_feed_mode(modes[(index + direction) % len(modes)])

    def action_prev_feed_tab(self):
        self._cycle_feed_tab(-1)

    def action_next_feed_tab(self):
        self._cycle_feed_tab(1)

    # -- loading ---------------------------------------------------------

    def _busy(self, text: str = "加载中…"):
        try:
            self.query_one("#bottombar", BottomBar).busy(text)
        except Exception:
            pass

    def _idle(self):
        try:
            self.query_one("#bottombar", BottomBar).idle()
        except Exception:
            pass

    async def _initial_load(self):
        await self._fill_viewport()
        hint = self.query("#hint")
        if hint:
            hint.first().remove()
        cards = self._cards()
        if cards:
            self._focus(cards[0])
        self.check_load_more()

    async def _fill_viewport(self):
        """Load pages until the feed overflows the viewport, so mouse
        wheel and scrollbar always have something to scroll."""
        for _ in range(8):
            if self.is_end:
                return
            await self._load_more()
            for _ in range(10):
                await asyncio.sleep(0.05)
                feed = self.query_one("#feed")
                if feed.max_scroll_y is not None and feed.max_scroll_y > 0:
                    return

    async def _load_more(self):
        if self._loading or self.is_end:
            return
        self._loading = True
        self._busy("加载中…")
        try:
            if self._mode == "history":
                entries = history.load()
                feed = self.query_one("#feed")
                if entries:
                    feed.mount(*(HistoryEntry(entry) for entry in entries))
                    feed.mount(HistoryEndRule())
                else:
                    feed.mount(Static("（暂无浏览历史）", classes="hint"))
                self.is_end = True
                return
            if self._mode == "hot":
                items, is_end = await asyncio.to_thread(self.client.hot)
            else:
                items, is_end = await asyncio.to_thread(self.client.feed)
            feed = self.query_one("#feed")
            cards = [self._make_card(it) for it in items]
            if cards:
                feed.mount(*cards)
                self._apply_layout()
            self.is_end = is_end
        except VerificationRequired as e:
            self.app.show_verification_required(str(e))
            self.is_end = True
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=10)
            self.is_end = True
        except Exception as e:
            self.notify(f"加载失败：{e}", severity="error", timeout=8)
        finally:
            self._loading = False
            self._idle()

    def check_load_more(self):
        if self._loading or self.is_end or self._resetting:
            return
        feed = self.query_one("#feed")
        if feed.max_scroll_y is None:
            return
        if feed.scroll_y >= feed.max_scroll_y - 8:
            self.run_worker(
                self._load_more(), name="feed-more", group="feed", exclusive=True
            )

    # -- navigation ------------------------------------------------------

    def _make_card(self, item):
        return build_card(self.client, item, self._view, self._show_images)

    def _cache_current(self):
        if self._mode in ("recommend", "hot"):
            self._mode_cache[self._mode] = {
                "items": [card.item for card in self._cards()],
                "is_end": self.is_end,
                "focus_index": self.focus_index,
                "scroll_y": self.query_one("#feed").scroll_y,
            }

    def _apply_layout(self):
        feed = self.query_one("#feed")
        grid = self._view == "grid" and kitty.enabled and self._mode != "history"
        feed.set_classes("grid-mode" if grid else "")
        if grid:
            feed.styles.grid_size_columns = grid_column_count(
                self.app.size.width or 100
            )

    def apply_grid_columns(self):
        if self._view == "grid" and kitty.enabled and self._mode != "history":
            self._apply_layout()

    def on_resize(self, event):
        if self._view == "grid" and self._mode != "history":
            try:
                self.query_one("#feed").styles.grid_size_columns = grid_column_count(
                    event.size.width
                )
            except Exception:
                pass

    def _cards(self) -> list:
        if self._mode == "history":
            return list(self.query(HistoryNoteRow))
        return [
            widget
            for widget in self.query_one("#feed").children
            if isinstance(widget, (Card, GridCard))
        ]

    def _focus_index(self) -> int:
        cards = self._cards()
        if not cards:
            return 0
        for i, c in enumerate(cards):
            if c.has_focus:
                return i
        return min(self.focus_index, len(cards) - 1)

    def _focus(self, card):
        card.focus()
        self.focus_index = self._cards().index(card)
        feed = self.query_one("#feed")
        if hasattr(feed, "scroll_to_widget"):
            feed.scroll_to_widget(card, animate=False, immediate=True)

    def action_next_card(self):
        cards = self._cards()
        if not cards:
            return
        i = min(self._focus_index() + 1, len(cards) - 1)
        self._focus(cards[i])

    def action_prev_card(self):
        cards = self._cards()
        if not cards:
            return
        i = max(self._focus_index() - 1, 0)
        self._focus(cards[i])

    def action_open_card(self):
        cards = self._cards()
        if not cards:
            return
        if self._mode == "history":
            self.open_history_note(cards[self._focus_index()])
            return
        index = self._focus_index()
        items = [card.item for card in cards]
        push_item_screen(
            self.app, self.client, cards[index].item, siblings=items, index=index
        )

    def open_history_note(self, row: HistoryNoteRow):
        self.run_worker(
            self._open_history_note(row),
            name="history-open", group="open", exclusive=True,
        )

    async def _open_history_note(self, row: HistoryNoteRow):
        note = row.note or {}
        nid = str(note.get("nid") or "")
        if not nid:
            self.notify("这条历史记录没有可重新打开的链接", timeout=4)
            return
        self._busy("正在打开…")
        try:
            item = await asyncio.to_thread(
                self.client.read, nid, str(note.get("xsec_token") or "")
            )
            push_item_screen(self.app, self.client, item)
        except VerificationRequired as exc:
            self.app.show_verification_required(str(exc))
        except NotLoggedIn as exc:
            self.notify(str(exc), severity="error", timeout=8)
        except Exception as exc:
            self.notify(f"历史记录打开失败：{exc}", severity="error", timeout=8)
        finally:
            self._idle()

    def action_open_browser(self):
        cards = self._cards()
        if not cards:
            return
        if self._mode == "history":
            if cards[self._focus_index()].url:
                webbrowser.open(cards[self._focus_index()].url)
            return
        item = cards[self._focus_index()].item
        if item.url:
            webbrowser.open(item.url)

    async def _reset_feed(self, use_cache: bool = False):
        self._resetting = True
        try:
            feed = self.query_one("#feed")
            await feed.remove_children()
            self.is_end = False
            self.focus_index = 0
            self._apply_layout()
            cached = self._mode_cache.get(self._mode) if use_cache else None
            if cached:
                cards = [self._make_card(item) for item in cached["items"]]
                if cards:
                    await feed.mount(*cards)
                    self._apply_layout()
                self.is_end = cached["is_end"]
                self.focus_index = min(cached["focus_index"], max(0, len(cards) - 1))
                if cards:
                    cards[self.focus_index].focus()
                feed.scroll_to(y=cached["scroll_y"], animate=False, immediate=True)
                return
            await self._initial_load()
        finally:
            self._resetting = False

    def action_refresh(self):
        self._mode_cache.pop(self._mode, None)
        self.run_worker(
            self._reset_feed(use_cache=False),
            name="feed-refresh", group="feed", exclusive=True,
        )

    # -- list images / grid view ------------------------------------------

    def action_toggle_images(self):
        if not kitty.enabled:
            self.notify(
                "终端不支持 Kitty 图片协议，卡片图片不可用",
                severity="warning",
                timeout=5,
            )
            return
        if self._view == "grid":
            self.notify("宫格视图始终显示图片，可在 Ctrl+P 中切回列表", timeout=4)
            return
        self._show_images = not self._show_images
        config.set_value("feed_show_images", self._show_images)
        self._cache_current()
        self.run_worker(
            self._reset_feed(use_cache=True), name="feed-view", group="feed",
            exclusive=True,
        )
        self.notify(f"卡片图片：{'开' if self._show_images else '关'}", timeout=3)

    def action_toggle_view(self):
        if not kitty.enabled:
            self.notify(
                "终端不支持 Kitty 图片协议，仅支持列表视图",
                severity="warning",
                timeout=5,
            )
            return
        if self._mode == "history":
            self.notify("历史页不支持宫格视图", timeout=3)
            return
        self._cache_current()
        self._view = "grid" if self._view == "list" else "list"
        config.set_value("feed_view", self._view)
        self.run_worker(
            self._reset_feed(use_cache=True), name="feed-view", group="feed",
            exclusive=True,
        )
        self.notify(f"视图：{'宫格' if self._view == 'grid' else '列表'}", timeout=3)

    def action_go_top(self):
        self.query_one("#feed").scroll_home(animate=False, immediate=True)

    def action_go_bottom(self):
        self.query_one("#feed").scroll_end(animate=False, immediate=True)

    def action_quit_app(self):
        self.app.exit()


class SearchScreen(Screen):
    """Search result list (Ctrl+S).  Cards behave like the feed; infinite
    scroll loads more results page by page."""

    BINDINGS = [
        Binding("j,down", "next_card", "下一条", show=False),
        Binding("k,up", "prev_card", "上一条", show=False),
        Binding("enter", "open_card", "展开"),
        Binding("space", "open_card", "", show=False),
        Binding("r", "refresh", "刷新"),
        Binding("o", "open_browser", "浏览器"),
        Binding("g", "go_top", "顶部", show=False),
        Binding("G", "go_bottom", "底部", show=False),
        Binding("escape,q", "back", "返回"),
    ]

    def __init__(self, client: XhsClient, query: str, user: dict | None = None):
        super().__init__()
        self.client = client
        self._query = query
        self.user = user or {}
        self._search_page = 1
        self.is_end = False
        self._loading = False
        self.focus_index = 0
        self._view = _initial_view()
        self._show_images = _initial_show_images()
        self._resetting = False

    def compose(self) -> ComposeResult:
        yield NoteTopBar(f"搜索 · {self._query}")
        yield FeedScroll(id="feed")
        yield BottomBar(id="bottombar")

    @on(Button.Pressed, "#note-back")
    def _search_back_button(self, event: Button.Pressed):
        self.action_back()

    def on_mount(self):
        self.run_worker(
            self._initial_load(), name="search-initial", group="search", exclusive=True
        )

    def _busy(self, text: str = "加载中…"):
        try:
            self.query_one("#bottombar", BottomBar).busy(text)
        except Exception:
            pass

    def _idle(self):
        try:
            self.query_one("#bottombar", BottomBar).idle()
        except Exception:
            pass

    async def _initial_load(self):
        await self._fill_viewport()
        hint = self.query("#hint")
        if hint:
            hint.first().remove()
        cards = self._cards()
        if cards:
            self._focus(cards[0])
        self.check_load_more()

    async def _fill_viewport(self):
        for _ in range(8):
            if self.is_end:
                return
            await self._load_more()
            for _ in range(10):
                await asyncio.sleep(0.05)
                feed = self.query_one("#feed")
                if feed.max_scroll_y is not None and feed.max_scroll_y > 0:
                    return

    async def _load_more(self):
        if self._loading or self.is_end:
            return
        self._loading = True
        self._busy("搜索中…")
        try:
            items, is_end = await asyncio.to_thread(
                self.client.search, self._query, self._search_page
            )
            feed = self.query_one("#feed")
            cards = [self._make_card(it) for it in items]
            if cards:
                feed.mount(*cards)
                self._apply_layout()
                self._search_page += 1
            self.is_end = is_end or not cards
            if not cards and self._search_page == 1 and not feed.children:
                feed.mount(Static(" （没有找到相关内容）", classes="hint"))
        except VerificationRequired as e:
            self.app.show_verification_required(str(e))
            self.is_end = True
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=10)
            self.is_end = True
        except Exception as e:
            self.notify(f"搜索失败：{e}", severity="error", timeout=8)
        finally:
            self._loading = False
            self._idle()

    def check_load_more(self):
        if self._loading or self.is_end or self._resetting:
            return
        feed = self.query_one("#feed")
        if feed.max_scroll_y is None:
            return
        if feed.scroll_y >= feed.max_scroll_y - 8:
            self.run_worker(
                self._load_more(), name="search-more", group="search", exclusive=True
            )

    def _make_card(self, item):
        return build_card(self.client, item, self._view, self._show_images)

    def _apply_layout(self):
        feed = self.query_one("#feed")
        grid = self._view == "grid" and kitty.enabled
        feed.set_classes("grid-mode" if grid else "")
        if grid:
            feed.styles.grid_size_columns = grid_column_count(
                self.app.size.width or 100
            )

    def apply_grid_columns(self):
        if self._view == "grid" and kitty.enabled:
            self._apply_layout()

    def on_resize(self, event):
        if self._view == "grid":
            try:
                self.query_one("#feed").styles.grid_size_columns = grid_column_count(
                    event.size.width
                )
            except Exception:
                pass

    def _cards(self) -> list:
        return [
            widget
            for widget in self.query_one("#feed").children
            if isinstance(widget, (Card, GridCard))
        ]

    def _focus_index(self) -> int:
        cards = self._cards()
        if not cards:
            return 0
        for i, c in enumerate(cards):
            if c.has_focus:
                return i
        return min(self.focus_index, len(cards) - 1)

    def _focus(self, card: Card):
        card.focus()
        self.focus_index = self._cards().index(card)
        feed = self.query_one("#feed")
        feed.scroll_to_widget(card, animate=False, immediate=True)

    def action_next_card(self):
        cards = self._cards()
        if not cards:
            return
        self._focus(cards[min(self._focus_index() + 1, len(cards) - 1)])

    def action_prev_card(self):
        cards = self._cards()
        if not cards:
            return
        self._focus(cards[max(self._focus_index() - 1, 0)])

    def action_open_card(self):
        cards = self._cards()
        if not cards:
            return
        index = self._focus_index()
        items = [card.item for card in cards]
        push_item_screen(
            self.app, self.client, cards[index].item, siblings=items, index=index
        )

    def action_open_browser(self):
        cards = self._cards()
        if not cards:
            return
        item = cards[self._focus_index()].item
        if item.url:
            webbrowser.open(item.url)

    def action_refresh(self):
        self._rebuild_view()

    # -- list images / grid view ------------------------------------------

    def action_toggle_images(self):
        if not kitty.enabled:
            self.notify(
                "终端不支持 Kitty 图片协议，卡片图片不可用",
                severity="warning",
                timeout=5,
            )
            return
        if self._view == "grid":
            self.notify("宫格视图始终显示图片，可在 Ctrl+P 中切回列表", timeout=4)
            return
        self._show_images = not self._show_images
        config.set_value("feed_show_images", self._show_images)
        self._rebuild_view()
        self.notify(f"卡片图片：{'开' if self._show_images else '关'}", timeout=3)

    def action_toggle_view(self):
        if not kitty.enabled:
            self.notify(
                "终端不支持 Kitty 图片协议，仅支持列表视图",
                severity="warning",
                timeout=5,
            )
            return
        self._view = "grid" if self._view == "list" else "list"
        config.set_value("feed_view", self._view)
        self._rebuild_view()
        self.notify(f"视图：{'宫格' if self._view == 'grid' else '列表'}", timeout=3)

    def _rebuild_view(self):
        self.run_worker(
            self._rebuild(), name="search-view", group="search", exclusive=True
        )

    async def _rebuild(self):
        self._resetting = True
        try:
            feed = self.query_one("#feed")
            await feed.remove_children()
            self._search_page = 1
            self.is_end = False
            self.focus_index = 0
            self._apply_layout()
            await self._initial_load()
        finally:
            self._resetting = False

    def action_go_top(self):
        self.query_one("#feed").scroll_home(animate=False, immediate=True)

    def action_go_bottom(self):
        self.query_one("#feed").scroll_end(animate=False, immediate=True)

    def action_back(self):
        self.app.pop_screen()

    def action_quit_app(self):
        self.app.exit()
