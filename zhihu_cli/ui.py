"""Textual UI: recommendation feed cards + expandable content + inline images."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import webbrowser
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import (
    Horizontal,
    ScrollableContainer,
    Vertical,
    VerticalScroll,
)
from textual.events import Click
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Button, Input, Rule, Static

from . import config, history, kitty
from .htmlparse import Block, find_urls, parse_html, table_to_text
from .net import (
    FeedItem,
    NotLoggedIn,
    ZhihuSession,
    fmt_num,
    fmt_time,
)

# 图片按钮改用 emoji（用户指定；NF 的 MDI 码点在部分终端仍显示异常）
ICON_IMAGE = "🖼"          # emoji: frame with picture
ICON_IMAGES = "🖼"         # emoji: frame with picture
ICON_LINK = "\U000f0337"   # nf-md-link (v3)
ICON_PLAY = "\U000f040a"   # nf-md-play (v3)
ICON_COMMENT = "\U000f017a"  # nf-md-comment (v3)

BADGE_COLOR = {
    "回答": "#3b82f6",
    "文章": "#10b981",
    "视频": "#f43f5e",
    "提问": "#a78bfa",
    "想法": "#f59e0b",
}

# Theme extension point. Add Theme(...) objects here; they are registered at
# startup and automatically appear in Textual's theme picker opened from GO.
CUSTOM_THEMES: tuple[Theme, ...] = (
    Theme(
        name="zhihu-dark",
        primary="#2563eb",
        secondary="#7c3aed",
        accent="#38bdf8",
        success="#16a34a",
        warning="#eab308",
        error="#f43f5e",
        background="#0f1115",
        surface="#141821",
        panel="#171a21",
        foreground="#e2e8f0",
        dark=True,
    ),
)


def open_image_with_system(client, url: str) -> bool:
    """Download an image and open it with the system default viewer.

    Fallback for terminals without the kitty protocol.  Returns True on
    success (or when a viewer was launched).
    """
    try:
        data = client.fetch_image_bytes(url, timeout=30)
    except Exception as e:
        try:
            webbrowser.open(url)
        except Exception:
            pass
        raise e
    ext = ".png"
    try:
        from PIL import Image as PILImage
        from io import BytesIO

        im = PILImage.open(BytesIO(data))
        ext = ".png" if im.format in ("PNG", None) else f".{im.format.lower()}"
    except Exception:
        pass
    fd, path = tempfile.mkstemp(suffix=ext, prefix="zhihu-img-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", path])
        elif shutil.which("open"):
            subprocess.Popen(["open", path])
        else:
            webbrowser.open(f"file://{path}")
    except Exception:
        webbrowser.open(f"file://{path}")
    return True


def system_viewer_notice() -> str:
    return "终端不支持图片显示，已用系统默认看图程序打开"


# --------------------------------------------------------------------------
# Top bar
# --------------------------------------------------------------------------


class TopBar(Static):
    def __init__(self, title: str, right: str = ""):
        super().__init__("")
        self._title = title
        self._right = right

    def render(self) -> Text:
        t = Text(f" {self._title} ", style="bold white")
        if self._right:
            t.append(f"  ·  {self._right}", style="#8a93a3")
        return t


class AnswerTopBar(Horizontal):
    """Answer header chrome with a left-aligned back button."""

    def __init__(self, badge: str):
        super().__init__(classes="answer-topbar")
        self.badge = badge

    def compose(self) -> ComposeResult:
        yield Button(" 返回 ", id="answer-back")
        yield Static(f"展开 · {self.badge}", classes="answer-topbar-title")


class FeedTopBar(Horizontal):
    """Feed top bar: 推荐/热榜 tabs (one-line) + username right-aligned."""

    def __init__(self, user: dict, mode: str = "recommend", **kwargs):
        super().__init__(classes="topbar", **kwargs)
        self.user = user or {}
        self._mode = mode

    def compose(self) -> ComposeResult:
        yield Button(" 推荐 ", id="tab-rec", classes=self._tab_cls("recommend"))
        yield Button(" 热榜 ", id="tab-hot", classes=self._tab_cls("hot"))
        yield Button(" 历史 ", id="tab-history", classes=self._tab_cls("history"))
        yield Static(f" {self.user.get('name') or ''} ", classes="tb-user")

    def _tab_cls(self, mode: str) -> str:
        return "tab tab-active" if self._mode == mode else "tab"

    def set_mode(self, mode: str):
        self._mode = mode
        self.query_one("#tab-rec", Button).set_classes(self._tab_cls("recommend"))
        self.query_one("#tab-hot", Button).set_classes(self._tab_cls("hot"))
        self.query_one("#tab-history", Button).set_classes(self._tab_cls("history"))


class StatusLine(Static):
    """Small animated spinner shown in the bottom bar's left, while loading."""

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._spin = 0
        self._text = ""
        self._active = False

    def on_mount(self):
        self.display = False
        self.set_interval(0.12, self._tick)

    def busy(self, text: str = "加载中…"):
        self._text = text
        self._active = True
        self._spin = 0
        self.display = True
        self.update(f"{self.SPINNER[0]} {text}")

    def idle(self):
        self._active = False
        self.update("")
        self.display = False

    def _tick(self):
        if not self._active:
            return
        self._spin += 1
        self.update(f"{self.SPINNER[self._spin % len(self.SPINNER)]} {self._text}")


class BottomBar(Horizontal):
    """Clickable one-row controls with a right-aligned command button."""

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    # Fixed product colors (not terminal ANSI colors) distinguish each mode.
    MODE_COLORS = {
        "cursor": "#2563eb",
        "visual": "#7c3aed",
    }
    MODE_LABELS = {"normal": "NORMAL", "cursor": "CURSOR", "visual": "VISUAL"}

    def __init__(self, feed_brand: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.feed_brand = feed_brand
        self._busy_text = ""
        self._spin = 0

    def compose(self) -> ComposeResult:
        yield Button(" NORMAL ", id="bottom-mode")
        yield Static("", id="bottom-status")
        yield Horizontal(id="bottom-actions")
        yield Static("", classes="bottom-fill")
        yield Button(" GO ", id="bottom-go")

    async def on_mount(self):
        try:
            self.screen.bindings_updated_signal.subscribe(self, self._on_bindings)
        except Exception:
            pass
        self.set_interval(0.12, self._tick)
        actions = self.query_one("#bottom-actions", Horizontal)
        buttons = []
        seen: set[str] = set()
        for binding in getattr(self.screen, "BINDINGS", ()) or ():
            desc = getattr(binding, "description", "") or ""
            action = getattr(binding, "action", "")
            if not getattr(binding, "show", False) or not desc or action in seen:
                continue
            seen.add(action)
            button = Button(f" {desc} ", classes="bottom-action")
            button._screen_action = action
            buttons.append(button)
        if buttons:
            await actions.mount(*buttons)
        self.update_mode()

    def on_unmount(self):
        try:
            self.screen.bindings_updated_signal.unsubscribe(self)
        except Exception:
            pass

    def _on_bindings(self, screen) -> None:
        self.update_mode()

    def busy(self, text: str = "加载中…"):
        self._busy_text = text
        self._spin = 0
        self._update_status()

    def idle(self):
        self._busy_text = ""
        self._update_status()

    def _tick(self):
        if self._busy_text:
            self._spin += 1
            self._update_status()

    def _update_status(self):
        status = self.query_one("#bottom-status", Static)
        if self.feed_brand:
            mode_button = self.query_one("#bottom-mode", Button)
            if self._busy_text:
                mode_button.label = (
                    f" {self.SPINNER[self._spin % len(self.SPINNER)]} "
                    f"{self._busy_text} "
                )
            else:
                mode_button.label = " zhihu "
            status.update("")
            return
        status.update(
            f" {self.SPINNER[self._spin % len(self.SPINNER)]} {self._busy_text} "
            if self._busy_text else ""
        )

    def update_mode(self):
        if self.feed_brand:
            self._update_status()
            return
        mode = getattr(self.app, "mode", "normal")
        variables = self.app.get_css_variables()
        color = self.MODE_COLORS.get(
            mode,
            variables.get("panel-darken-1", variables.get("panel", "#171a21")),
        )
        label = self.MODE_LABELS.get(mode, "NORMAL")
        button = self.query_one("#bottom-mode", Button)
        button.label = f" {label} "
        button.styles.background = color

    @on(Button.Pressed, "#bottom-mode")
    def _cycle_mode(self, event: Button.Pressed):
        if self.feed_brand:
            return
        mode = getattr(self.app, "mode", "normal")
        screen = self.screen
        if mode == "normal" and hasattr(screen, "action_cursor_mode"):
            screen.action_cursor_mode()
        elif mode == "cursor":
            screen._exit_cursor_mode()
            self.app.action_select_mode()
        elif mode == "visual":
            self.app.action_select_mode()
        else:
            self.app.action_select_mode()
        self.update_mode()

    @on(Button.Pressed, ".bottom-action")
    def _run_screen_action(self, event: Button.Pressed):
        action = getattr(event.button, "_screen_action", "")
        if action:
            self.app.run_worker(self.screen.run_action(action))

    @on(Button.Pressed, "#bottom-go")
    def _open_go(self, event: Button.Pressed):
        self.app.action_command_palette()


class AnswerHeader(Vertical):
    """Fixed header: 问题 / 作者 / 赞 / 链接.  Ctrl+click opens the link."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.item: FeedItem | None = None

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Button("Q", id="answer-question"),
            Static("", id="answer-question-title"),
            classes="answer-question-row",
        )
        yield Static("", id="answer-meta")

    def set_item(self, item: FeedItem):
        self.item = item
        question_button = self.query_one("#answer-question", Button)
        question_button.display = item.kind == "answer" and bool(item.qid)
        self.query_one("#answer-question-title", Static).update(
            Text(item.title or "", style="bold")
        )
        t = Text()
        # 作者 · 赞 · 评论 · 时间
        t.append(f"{item.author or '匿名用户'}", style="bold #eab308")
        if item.votes:
            t.append(f"  赞 {fmt_num(item.votes)}", style="#34d399")
        if item.comments:
            t.append(f"  评 {fmt_num(item.comments)}", style="#8a93a3")
        if item.created:
            t.append(f"  {fmt_time(item.created)}", style="#8a93a3")
        if item.kind == "zvideo":
            t.append(f"  {ICON_PLAY} 视频", style="#f43f5e")
        t.append("\n")
        if item.url:
            t.append(f"{ICON_LINK} ", style="dim")
            t.append(item.url, style="underline #5eb4f8")
        self.query_one("#answer-meta", Static).update(t)

    def on_click(self, event: Click):
        if event.ctrl and self.item is not None and self.item.url:
            webbrowser.open(self.item.url)


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------


def push_item_screen(app: App, client: ZhihuSession, item: FeedItem) -> None:
    """Route a feed/search card to its type-specific screen."""
    if item.kind == "question":
        app.push_screen(QuestionScreen(client, item))
    else:
        app.push_screen(ContentScreen(client, item))


class Card(Static, can_focus=True):
    def __init__(self, item: FeedItem, show_title: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.item = item
        self.show_title = show_title
        self._search_highlight: tuple[int, int] | None = None

    def render(self) -> Text:
        it = self.item
        t = Text()
        # 热榜排序徽标（左上角）
        if it.rank:
            rcolor = "#f43f5e" if it.rank <= 3 else "#eab308" if it.rank <= 10 else "#64748b"
            t.append(f" {it.rank} ", style=f"bold white on {rcolor}")
            t.append("  ")
        color = BADGE_COLOR.get(it.badge, "#64748b")
        t.append(f" {it.badge} ", style=f"bold white on {color}")
        if self.show_title:
            t.append("  ")
            t.append(it.title, style="bold")
        t.append("\n")
        if it.excerpt:
            t.append(it.excerpt, style="#94a3b8")
            t.append("\n")
        t.append(f" {it.author or '匿名用户'}", style="bold #eab308")
        if it.votes:
            t.append(f"  赞 {fmt_num(it.votes)}", style="#34d399")
        if it.comments:
            t.append(f"  评 {fmt_num(it.comments)}", style="#8a93a3")
        if it.created:
            t.append(f"  {fmt_time(it.created)}", style="#8a93a3")
        if it.images:
            t.append(f"  {ICON_IMAGE} {len(it.images)}", style="#38bdf8")
        if it.kind == "zvideo":
            t.append(f"  {ICON_PLAY} 视频", style="#f43f5e")
        if self._search_highlight:
            t.stylize("bold black on #fbbf24", *self._search_highlight)
        return t

    def set_search_highlight(self, span: tuple[int, int] | None):
        self._search_highlight = span
        self.refresh()

    def on_click(self, event):
        self.focus()
        push_item_screen(self.app, self.app.client, self.item)


def _truncate_cells(value: str, width: int) -> tuple[str, bool]:
    out = ""
    used = 0
    for char in value:
        char_width = cell_len(char)
        if used + char_width > width:
            return out, True
        out += char
        used += char_width
    return out, False


class HistoryAnswerRow(Static, can_focus=True):
    """A single selectable answer from local history."""

    def __init__(self, answer: dict, index: int, **kwargs):
        super().__init__(**kwargs)
        self.answer = answer
        self.index = index
        self.url = answer.get("url") or ""

    def render(self) -> Text:
        badge = f" {self.index} "
        author = self.answer.get("author") or "匿名用户"
        author_text = f" {author}："
        prefix = badge + author_text
        excerpt = " ".join((self.answer.get("excerpt") or "（无摘要）").split())
        width = max(8, self.size.width - 1)
        primary = self.app.get_css_variables().get("primary", "#2563eb")
        muted = self.app.get_css_variables().get("text-muted", "#8a93a3")
        created = int(self.answer.get("created") or 0)
        date = time.strftime("%Y-%m-%d", time.localtime(created)) if created else ""
        votes = fmt_num(self.answer.get("votes") or 0)
        comments = fmt_num(self.answer.get("comments") or 0)
        images = int(self.answer.get("images") or 0)
        suffix = f"赞 {votes}  评 {comments}  {ICON_IMAGE} {images}"
        if date:
            suffix += f"  |  {date}"

        # Keep the author and right metadata visible; the excerpt yields its
        # space first and receives a muted ellipsis when truncated.
        prefix_text, prefix_cut = _truncate_cells(prefix, width)
        # Start with an unstyled root Text. Giving the root a background
        # would make every subsequently appended span inherit the badge blue.
        t = Text()
        t.append(badge, style=f"bold white on {primary}")
        t.append(prefix_text[len(badge):], style="bold #eab308")
        if prefix_cut:
            t.append("…", style=muted)
            return t

        room = width - cell_len(prefix_text) - cell_len(suffix) - 2
        shown, cut = _truncate_cells(excerpt, max(0, room - 1))
        t.append(shown)
        if cut or cell_len(excerpt) > cell_len(shown):
            t.append("…", style=muted)
        occupied = cell_len(t.plain) + cell_len(suffix)
        t.append(" " * max(1, width - occupied))
        t.append(f"赞 {votes}", style="#34d399")
        t.append(f"  评 {comments}", style=muted)
        t.append(f"  {ICON_IMAGE} {images}", style="#38bdf8")
        if date:
            t.append(f"  |  {date}", style=muted)
        return t

    def on_click(self, event: Click):
        self.focus()
        if hasattr(self.screen, "open_history_row"):
            self.screen.open_history_row(self)


class HistoryHeading(Static):
    def __init__(self, entry: dict, **kwargs):
        super().__init__(**kwargs)
        self.entry = entry

    def render(self) -> Text:
        width = max(1, self.size.width)
        title = self.entry.get("title") or "（无标题问题）"
        when = fmt_time(self.entry.get("viewed"))
        primary = self.app.get_css_variables().get("primary", "#2563eb")
        muted = self.app.get_css_variables().get("text-muted", "#8a93a3")
        title_width = max(1, width - cell_len(when) - 4)
        shown, cut = _truncate_cells(title, title_width)
        t = Text("═" * width, style=muted)
        t.append("\n◆ ", style=primary)
        t.append(shown, style="bold")
        if cut:
            t.append("…", style=muted)
        occupied = 2 + cell_len(shown) + (1 if cut else 0)
        t.append(" " * max(1, width - occupied - cell_len(when)))
        t.append(when, style=muted)
        t.append("\n" + "─" * width, style=muted)
        return t


class HistoryEntry(Vertical):
    """Compact history group; adjacent groups share one double rule."""

    def __init__(self, entry: dict, **kwargs):
        super().__init__(**kwargs)
        self.entry = entry

    def compose(self) -> ComposeResult:
        entry = self.entry
        yield HistoryHeading(entry, classes="history-heading")
        answers = entry.get("answers") or []
        if not answers:
            yield Static("（尚未浏览回答）", classes="history-empty")
        for index, answer in enumerate(answers, 1):
            yield HistoryAnswerRow(answer, index)


class HistoryEndRule(Static):
    def render(self) -> Text:
        muted = self.app.get_css_variables().get("text-muted", "#8a93a3")
        return Text("═" * max(1, self.size.width), style=muted)


class PageSearchBar(Horizontal):
    """One-row regular-expression search UI shared by content pages."""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="搜索（正则）", id="page-search-input")
        yield Static("", id="page-search-status")
        yield Button(" h ← ", classes="page-search-up")
        yield Button(" → l ", classes="page-search-down")
        yield Button(" × ", classes="page-search-close")

    def on_mount(self):
        self.display = False


def _open_page_search(screen: Screen) -> None:
    screen.app.clear_notifications()
    bar = screen.query_one(PageSearchBar)
    bar.display = True
    screen.query_one("#page-search-input", Input).focus()


def _set_search_highlight(widget: Static, span: tuple[int, int] | None) -> None:
    if hasattr(widget, "set_search_highlight"):
        widget.set_search_highlight(span)
        return
    # Plain Static blocks (math/code/table text) can be highlighted by
    # temporarily replacing their Rich Text renderable, then restoring it.
    if span is None:
        original = getattr(widget, "_page_search_original", None)
        if original is not None:
            widget.update(original)
            widget._page_search_original = None
        return
    rendered = widget.render()
    if not isinstance(rendered, Text):
        return
    if getattr(widget, "_page_search_original", None) is None:
        widget._page_search_original = rendered.copy()
    highlighted = widget._page_search_original.copy()
    highlighted.stylize("bold black on #fbbf24", *span)
    widget.update(highlighted)


def _close_page_search(screen: Screen) -> bool:
    bar = screen.query_one(PageSearchBar)
    if not bar.display:
        return False
    for widget in screen.query(Static):
        _set_search_highlight(widget, None)
    bar.display = False
    screen._page_search_matches = []
    screen._page_search_pattern = None
    screen._page_search_match = -1
    return True


def _find_in_widgets(screen: Screen, widgets: list[Static], container, direction: int) -> None:
    value = screen.query_one("#page-search-input", Input).value
    if not value:
        return
    try:
        pattern = re.compile(value, re.IGNORECASE)
    except re.error as exc:
        screen.query_one("#page-search-status", Static).update(f" 正则无效：{exc} ")
        return
    matches: list[tuple[Static, int, int]] = []
    for widget in widgets:
        rendered = widget.render()
        plain = rendered.plain if isinstance(rendered, Text) else str(rendered)
        for match in pattern.finditer(plain):
            if match.end() > match.start():
                matches.append((widget, match.start(), match.end()))
    if not matches:
        screen.query_one("#page-search-status", Static).update(" 0/0 ")
        return
    if getattr(screen, "_page_search_pattern", None) != value:
        screen._page_search_pattern = value
        screen._page_search_match = -1 if direction > 0 else 0
    old = getattr(screen, "_page_search_match", -1)
    index = (old + direction) % len(matches)
    screen._page_search_match = index
    for widget in widgets:
        _set_search_highlight(widget, None)
    target, start, end = matches[index]
    _set_search_highlight(target, (start, end))
    container.scroll_to_widget(target, animate=False, immediate=True)
    if target.can_focus:
        target.focus()
    screen._page_search_matches = matches
    screen.query_one("#page-search-status", Static).update(f" {index + 1}/{len(matches)} ")


class FeedScroll(VerticalScroll, inherit_bindings=False):
    # Don't let up/down/left/right be consumed by the scroll container:
    # those keys move the card selection (screen bindings).  Page/Home/End
    # still scroll the viewport (instantly).  Mouse wheel is unaffected.
    BINDINGS = [
        Binding("pageup", "instant_page_up", "", show=False),
        Binding("pagedown", "instant_page_down", "", show=False),
        Binding("home", "instant_home", "", show=False),
        Binding("end", "instant_end", "", show=False),
    ]

    def action_instant_page_up(self):
        self.scroll_to(
            y=self.scroll_y - self.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )

    def action_instant_page_down(self):
        self.scroll_to(
            y=self.scroll_y + self.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )

    def action_instant_home(self):
        self.scroll_home(animate=False, immediate=True)

    def action_instant_end(self):
        self.scroll_end(animate=False, immediate=True)

    def watch_scroll_y(self, old, new):
        super().watch_scroll_y(old, new)
        if hasattr(self.screen, "check_load_more"):
            self.screen.check_load_more()


class FeedScreen(Screen):
    BINDINGS = [
        Binding("comma,less_than_sign", "prev_feed_tab", "", show=False),
        Binding("full_stop,greater_than_sign", "next_feed_tab", "", show=False),
        Binding("j,down", "next_card", "下一条", show=False),
        Binding("k,up", "prev_card", "上一条", show=False),
        Binding("enter", "open_card", "展开"),
        Binding("r", "refresh", "刷新"),
        Binding("o", "open_browser", "浏览器"),
        Binding("g", "go_top", "顶部", show=False),
        Binding("G", "go_bottom", "底部", show=False),
        Binding("q,ctrl+c", "quit_app", "退出"),
    ]

    def __init__(self, client: ZhihuSession, user: dict):
        super().__init__()
        self.client = client
        self.user = user or {}
        self.after_id = ""
        self.page = 1
        self.is_end = False
        self._loading = False
        self.focus_index = 0
        self._mode = "recommend"  # recommend | hot | history
        self._mode_cache: dict[str, dict] = {}

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
            self._mode_cache[self._mode] = {
                "items": [card.item for card in self.query(Card)],
                "after_id": self.after_id,
                "page": self.page,
                "is_end": self.is_end,
                "focus_index": self.focus_index,
                "scroll_y": self.query_one("#feed").scroll_y,
            }
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
        self.query_one("#bottombar", BottomBar).busy(text)

    def _idle(self):
        self.query_one("#bottombar", BottomBar).idle()

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
                items, is_end = await asyncio.to_thread(self.client.hot_list)
            else:
                items, is_end = await asyncio.to_thread(
                    self.client.feed, self.after_id, self.page
                )
            feed = self.query_one("#feed")
            cards = [Card(it) for it in items]
            if cards:
                feed.mount(*cards)
            if items and self._mode == "recommend":
                self.after_id = items[-1].rid
                self.page += 1
            self.is_end = is_end
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=10)
            self.is_end = True
        except Exception as e:
            self.notify(f"加载失败：{e}", severity="error", timeout=8)
        finally:
            self._loading = False
            self._idle()

    def check_load_more(self):
        if self._loading or self.is_end:
            return
        feed = self.query_one("#feed")
        if feed.max_scroll_y is None:
            return
        if feed.scroll_y >= feed.max_scroll_y - 8:
            self.run_worker(
                self._load_more(), name="feed-more", group="feed", exclusive=True
            )

    # -- navigation ------------------------------------------------------

    def _cards(self) -> list:
        if self._mode == "history":
            return list(self.query(HistoryAnswerRow))
        return list(self.query(Card))

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
            self.open_history_row(cards[self._focus_index()])
            return
        item = cards[self._focus_index()].item
        push_item_screen(self.app, self.client, item)

    def open_history_row(self, row: HistoryAnswerRow):
        if not row.url:
            self.notify("这条历史记录没有可重新打开的链接", timeout=4)
            return
        self.run_worker(
            self._open_history_url(row.url),
            name="history-open", group="open", exclusive=True,
        )

    async def _open_history_url(self, url: str):
        self._busy("正在打开…")
        try:
            item = await asyncio.to_thread(self.client.fetch_url, url)
            push_item_screen(self.app, self.client, item)
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
        feed = self.query_one("#feed")
        await feed.remove_children()
        self.after_id = ""
        self.page = 1
        self.is_end = False
        self.focus_index = 0
        cached = self._mode_cache.get(self._mode) if use_cache else None
        if cached:
            cards = [Card(item) for item in cached["items"]]
            if cards:
                await feed.mount(*cards)
            self.after_id = cached["after_id"]
            self.page = cached["page"]
            self.is_end = cached["is_end"]
            self.focus_index = min(cached["focus_index"], max(0, len(cards) - 1))
            if cards:
                cards[self.focus_index].focus()
            feed.scroll_to(y=cached["scroll_y"], animate=False, immediate=True)
            return
        await self._initial_load()

    def action_refresh(self):
        self._mode_cache.pop(self._mode, None)
        self.run_worker(
            self._reset_feed(use_cache=False),
            name="feed-refresh", group="feed", exclusive=True,
        )

    def action_go_top(self):
        self.query_one("#feed").scroll_home(animate=False, immediate=True)

    def action_go_bottom(self):
        self.query_one("#feed").scroll_end(animate=False, immediate=True)

    def action_quit_app(self):
        self.app.exit()


class SearchScreen(Screen):
    """Search result list (Ctrl+S).  Cards behave like the feed; infinite
    scroll loads more results via offset."""

    BINDINGS = [
        Binding("j,down", "next_card", "下一条", show=False),
        Binding("k,up", "prev_card", "上一条", show=False),
        Binding("enter", "open_card", "展开"),
        Binding("r", "refresh", "刷新"),
        Binding("o", "open_browser", "浏览器"),
        Binding("g", "go_top", "顶部", show=False),
        Binding("G", "go_bottom", "底部", show=False),
        Binding("escape", "back", "返回"),
        Binding("q,ctrl+c", "quit_app", "退出"),
    ]

    def __init__(self, client: ZhihuSession, query: str, user: dict | None = None):
        super().__init__()
        self.client = client
        self._query = query
        self.user = user or {}
        self._search_offset = 0
        self.is_end = False
        self._loading = False
        self.focus_index = 0

    def compose(self) -> ComposeResult:
        yield TopBar(f"搜索 · {self._query}")
        yield FeedScroll(id="feed")
        yield BottomBar(id="bottombar")

    def on_mount(self):
        self.run_worker(
            self._initial_load(), name="search-initial", group="search", exclusive=True
        )

    def _busy(self, text: str = "加载中…"):
        self.query_one("#bottombar", BottomBar).busy(text)

    def _idle(self):
        self.query_one("#bottombar", BottomBar).idle()

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
            items, is_end, self._search_offset = await asyncio.to_thread(
                self.client.search, self._query, self._search_offset
            )
            feed = self.query_one("#feed")
            cards = [Card(it) for it in items]
            if cards:
                feed.mount(*cards)
            self.is_end = is_end
            if not cards:
                self.is_end = True
                if self._search_offset == len(items) and not self.query_one("#feed").children:
                    feed.mount(Static(" （没有找到相关内容）", classes="hint"))
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=10)
            self.is_end = True
        except Exception as e:
            self.notify(f"搜索失败：{e}", severity="error", timeout=8)
        finally:
            self._loading = False
            self._idle()

    def check_load_more(self):
        if self._loading or self.is_end:
            return
        feed = self.query_one("#feed")
        if feed.max_scroll_y is None:
            return
        if feed.scroll_y >= feed.max_scroll_y - 8:
            self.run_worker(
                self._load_more(), name="search-more", group="search", exclusive=True
            )

    def _cards(self) -> list[Card]:
        return list(self.query(Card))

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
        push_item_screen(self.app, self.client, cards[self._focus_index()].item)

    def action_open_browser(self):
        cards = self._cards()
        if not cards:
            return
        item = cards[self._focus_index()].item
        if item.url:
            webbrowser.open(item.url)

    def action_refresh(self):
        self.query_one("#feed").remove_children()
        self._search_offset = 0
        self.is_end = False
        self.focus_index = 0
        self.run_worker(
            self._initial_load(), name="search-initial", group="search", exclusive=True
        )

    def action_go_top(self):
        self.query_one("#feed").scroll_home(animate=False, immediate=True)

    def action_go_bottom(self):
        self.query_one("#feed").scroll_end(animate=False, immediate=True)

    def action_back(self):
        self.app.pop_screen()

    def action_quit_app(self):
        self.app.exit()


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

    def compose(self) -> ComposeResult:
        yield AnswerTopBar("提问")
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


# --------------------------------------------------------------------------
# Inline images (kitty graphics protocol)
# --------------------------------------------------------------------------


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


class ClickableText(Static, can_focus=True):
    """A text block whose link spans open in the browser on click.

    ``links`` is a list of ``(start, end, url)`` into the plain text of
    ``text``.  Click (or Ctrl+Click) on a link opens it; in cursor mode
    ``activate()`` opens the first link.
    """

    def __init__(self, text, links=None, **kwargs):
        super().__init__(text, **kwargs)
        self._rich = text if isinstance(text, Text) else Text(str(text))
        self._links = links or []
        self._search_highlight: tuple[int, int] | None = None

    def render(self) -> Text:
        """Apply link decoration from the active Textual theme at render time."""
        text = self._rich.copy()
        variables = self.app.get_css_variables()
        color = variables.get("link-color", variables.get("primary", "blue"))
        background = variables.get(
            "link-background", variables.get("primary-background", "black")
        )
        for start, end, _url in self._links:
            text.stylize(f"underline {color} on {background}", start, end)
        if self._search_highlight:
            text.stylize("bold black on #fbbf24", *self._search_highlight)
        return text

    def set_search_highlight(self, span: tuple[int, int] | None):
        self._search_highlight = span
        self.refresh()

    def _lines(self) -> list[tuple[int, int, str]]:
        """Return [(char_start, char_end_exclusive, line_text)] after
        wrapping to the widget width (cell-width aware)."""
        width = max(1, self.size.width)
        plain = self._rich.plain
        lines: list[tuple[int, int, str]] = []
        cur: list[str] = []
        cur_w = 0
        start = 0
        for i, ch in enumerate(plain):
            w = cell_len(ch)
            if cur and cur_w + w > width:
                lines.append((start, i, "".join(cur)))
                cur, cur_w, start = [], 0, i
            cur.append(ch)
            cur_w += w
        if cur or not lines:
            lines.append((start, len(plain), "".join(cur)))
        return lines

    def _index_at(self, x: int, y: int) -> int | None:
        lines = self._lines()
        if y < 0 or y >= len(lines):
            return None
        start, _end, line = lines[y]
        cells = 0
        for i, ch in enumerate(line):
            w = cell_len(ch)
            if cells + w > x:
                return start + i
            cells += w
        return start + len(line) - 1

    def _url_at(self, x: int, y: int) -> str | None:
        idx = self._index_at(x, y)
        if idx is None:
            return None
        for s, e, url in self._links:
            if s <= idx < e:
                return url
        return None

    def on_click(self, event: Click):
        if event.button != 1:
            return
        url = self._url_at(event.x, event.y)
        if url:
            webbrowser.open(url)
            event.stop()

    def activate(self):
        """Cursor-mode activation: open the first link in this block."""
        if self._links:
            webbrowser.open(self._links[0][2])


class LinkButton(Button):
    """Two-line link card: parsed title first, URL second."""

    def __init__(self, url: str, title: str = "", **kwargs):
        clean_title = " ".join((title or "").split()).strip()
        label = Text()
        if clean_title and clean_title != url:
            label.append(f" {ICON_LINK}  {clean_title}", style="#7dd3fc")
            label.append("\n")
            label.append(f"    {url}", style="underline #7dd3fc")
        else:
            label.append(f" {ICON_LINK}  {url}", style="underline #7dd3fc")
        super().__init__(label, classes="linkbtn", **kwargs)
        self.url = url


class ContentScroll(VerticalScroll):
    # instant scroll actions (no animation lag)
    def action_scroll_up(self):
        self.scroll_up(animate=False, immediate=True)

    def action_scroll_down(self):
        self.scroll_down(animate=False, immediate=True)

    def action_page_up(self):
        self.scroll_to(
            y=self.scroll_y - self.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )

    def action_page_down(self):
        self.scroll_to(
            y=self.scroll_y + self.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )

    def action_scroll_home(self):
        self.scroll_home(animate=False, immediate=True)

    def action_scroll_end(self):
        self.scroll_end(animate=False, immediate=True)

    def watch_scroll_x(self, old, new):
        super().watch_scroll_x(old, new)

    def watch_scroll_y(self, old, new):
        super().watch_scroll_y(old, new)
        s = self.screen
        if hasattr(s, "check_bottom"):
            s.check_bottom()


class ContentScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "返回", show=False),
        Binding("j,down", "cursor_down", "", show=False),
        Binding("k,up", "cursor_up", "", show=False),
        Binding("h,left", "cursor_left", "", show=False),
        Binding("l,right", "cursor_right", "", show=False),
        Binding("space", "page_down", "下翻页", show=False),
        Binding("enter", "cursor_activate", "打开", show=False),
        Binding("n", "next_answer", "下一个回答"),
        Binding("N", "prev_answer", "上一个回答"),
        Binding("c", "comments", "评论"),
        Binding("o", "open_browser", "浏览器"),
        Binding("i", "cursor_mode", "光标模式", show=False),
        Binding("a", "toggle_all_images", "全部图片", show=False),
        Binding("/", "page_search", "", show=False),
    ]

    def __init__(self, client: ZhihuSession, item: FeedItem):
        super().__init__()
        self.client = client
        self.item = item
        self.img_sections: list[ImageSection] = []
        self._rendering = False
        self._bottom_notified = False
        self._cursor_mode = False
        # answer switching state
        self._answers: list[FeedItem] = []
        self._ans_idx: int = -1
        self._ans_offset: int = 0
        self._ans_end: bool = False
        self._ans_ready: bool = False
        self._ans_switching: bool = False

    def compose(self) -> ComposeResult:
        yield AnswerTopBar(self.item.badge)
        yield AnswerHeader(id="answer-header")
        yield ContentScroll(id="content")
        yield PageSearchBar(id="page-search")
        yield BottomBar(id="bottombar")

    def on_mount(self):
        self.run_worker(self._build(), name="content-build")

    def on_unmount(self):
        if self._cursor_mode:
            self.app.set_cursor_mode_active(False)

    def _busy(self, text: str = "加载中…"):
        self.query_one("#bottombar", BottomBar).busy(text)

    def _idle(self):
        self.query_one("#bottombar", BottomBar).idle()

    # -- content ----------------------------------------------------------

    async def _build(self):
        await self._render_item(self.item)

    async def _render_item(self, item: FeedItem):
        while self._rendering:
            await asyncio.sleep(0.05)
        self._rendering = True
        self._busy("加载中…")
        try:
            try:
                await self._render_body(item)
            except Exception as e:
                self.notify(f"内容解析失败：{e}", severity="error", timeout=8)
                content = self.query_one("#content")
                content.mount(Static(Text(f"（内容解析失败：{e}）", style="dim")))
                content.scroll_home(animate=False, immediate=True)
        finally:
            self._rendering = False
            self._idle()

    async def _render_body(self, item: FeedItem):
        self.item = item
        try:
            await asyncio.to_thread(history.record, item)
        except Exception:
            pass
        self._bottom_notified = False
        header = self.query_one("#answer-header", AnswerHeader)
        header.set_item(item)
        content = self.query_one("#content")
        self.img_sections.clear()
        await content.remove_children()
        it = item
        content.mount(Rule(classes="rule"))

        def add_img_section(urls, caption=""):
            sec = ImageSection(
                self.client, urls, len(self.img_sections) + 1, caption
            )
            self.img_sections.append(sec)
            content.mount(sec)

        if it.kind == "zvideo":
            note = f"{ICON_PLAY} 视频内容请在浏览器中播放"
            content.mount(Static(Text(note, style="italic #f43f5e")))
            if it.thumb:
                add_img_section([it.thumb], "视频封面")
            play = Button(f" {ICON_PLAY} 播放视频 ", id="open-video", classes="playbtn")
            content.mount(Horizontal(play, classes="row"))
        else:
            blocks: list[Block] = []
            if it.content:
                blocks, _ = await asyncio.to_thread(parse_html, it.content)
            if it.kind == "pin":
                for u in it.images:
                    add_img_section([u])
            for b in blocks:
                if b.kind == "img":
                    add_img_section(b.urls, b.caption)
                elif b.kind == "math":
                    content.mount(Static(b.text, classes="math"))
                elif b.kind == "list":
                    text = Text()
                    links: list[tuple[int, int, str]] = []
                    for item_text, item_links in zip(b.items, b.item_links):
                        start = len(text.plain)
                        text.append_text(item_text)
                        text.append("\n")
                        for s, e, url in item_links:
                            links.append((start + s, start + e, url))
                    link_only = self._mount_link_buttons(content, b, links, text)
                    if not link_only:
                        content.mount(ClickableText(text, links=links))
                elif b.kind == "table":
                    tt = table_to_text(b)
                    lines = (tt.plain or "").split("\n")
                    maxw = max((cell_len(ln) for ln in lines), default=0)
                    avail = max(30, (self.app.size.width or 80) - 4)
                    if maxw > avail:
                        n_rows = max(0, len(b.rows) - 1)
                        n_cols = max((len(r) for r in b.rows), default=0)
                        btn = Button(
                            f" 展开表格（{n_rows} 行 × {n_cols} 列） ", classes="tablebtn"
                        )
                        btn._table_text = tt
                        content.mount(btn)
                    else:
                        content.mount(Static(tt))
                elif b.kind == "quote":
                    link_only = self._mount_link_buttons(content, b, b.links, b.text)
                    if not link_only:
                        content.mount(ClickableText(b.text, links=b.links, classes="quoteblock"))
                elif b.kind == "code":
                    content.mount(Static(b.text, classes="codeblock"))
                elif b.kind == "hr":
                    content.mount(Static(b.text))
                else:
                    if b.text:
                        link_only = self._mount_link_buttons(content, b, b.links, b.text)
                        if not link_only:
                            content.mount(ClickableText(b.text, links=b.links))
            if not blocks and not it.images:
                content.mount(Static(Text("（无正文内容）", style="dim")))

        # Individual viewers browse this answer's images only.
        answer_urls: list[str] = []
        for sec in self.img_sections:
            for url in sec.urls:
                if url not in answer_urls:
                    answer_urls.append(url)
        for sec in self.img_sections:
            sec.viewer_urls = answer_urls
            sec.viewer_index = answer_urls.index(sec.urls[0])
            if config.get("expand_images_by_default", False) and kitty.enabled:
                sec.run_worker(sec._load_inline(), name=f"inline-img-{sec.no}")
        row = Horizontal(Button(" 在浏览器打开 ", id="open-web"), classes="row")
        content.mount(row)
        content.scroll_home(animate=False, immediate=True)
    @on(Button.Pressed, "#open-web")
    def _open_web(self, event: Button.Pressed):
        if self.item.url:
            webbrowser.open(self.item.url)

    @on(Button.Pressed, "#answer-back")
    def _answer_back(self, event: Button.Pressed):
        self._leave_screen()

    @on(Button.Pressed, "#answer-question")
    def _answer_question(self, event: Button.Pressed):
        if self.item.qid:
            self.run_worker(
                self._open_question(), name="open-question", group="open", exclusive=True
            )

    async def _open_question(self):
        try:
            question = await asyncio.to_thread(
                self.client.fetch_url,
                f"https://www.zhihu.com/question/{self.item.qid}",
            )
            self.app.push_screen(QuestionScreen(self.client, question))
        except Exception as e:
            self.notify(f"问题加载失败：{e}", severity="error", timeout=8)

    @on(Button.Pressed, "#open-video")
    def _open_video(self, event: Button.Pressed):
        url = self.item.video_url or self.item.url
        if url:
            webbrowser.open(url)

    @on(Button.Pressed, ".linkbtn")
    def _open_link(self, event: Button.Pressed):
        btn = event.button
        url = getattr(btn, "url", "")
        if url:
            webbrowser.open(url)

    @on(Button.Pressed, ".tablebtn")
    def _open_table(self, event: Button.Pressed):
        tt = getattr(event.button, "_table_text", None)
        if tt is not None:
            self.app.push_screen(TableScreen(tt))

    def _mount_link_buttons(self, content, block: Block, links, text_obj) -> bool:
        """Mount a one-line 打开链接 button ahead of every unique URL found
        in the block: both explicit <a> hrefs and bare URLs found by the
        strict URL regex. Return True when the block contains links only, so
        callers can omit the duplicate plain-text rendering."""
        urls: list[str] = []
        plain = text_obj.plain if isinstance(text_obj, Text) else str(text_obj)
        spans = list(links or [])
        labels: dict[str, str] = {}
        for s, e, u in spans:
            if u and u not in urls:
                urls.append(u)
            if u:
                labels.setdefault(u, plain[max(0, s):min(len(plain), e)].strip())
        bare_spans = find_urls(plain)
        for s, e, u in bare_spans:
            if u and u not in urls:
                urls.append(u)
        for u in urls:
            content.mount(LinkButton(u, labels.get(u, "")))
        covered = [False] * len(plain)
        for s, e, _u in spans + bare_spans:
            for i in range(max(0, s), min(len(plain), e)):
                covered[i] = True
        residual = "".join(ch for i, ch in enumerate(plain) if not covered[i])
        return bool(urls) and not residual.strip(" \t\r\n·:：-—()（）[]【】")

    # -- answer switching -------------------------------------------------

    async def _ensure_answers(self):
        """Load the question's answer list (default sort) and locate the
        current answer inside the first page when possible. A low-ranked
        current answer need not be fetched before switching to another one."""
        if self._ans_ready:
            return
        qid = self.item.qid
        if not qid:
            match = re.search(r"/question/(\d+)", self.item.url or "")
            qid = match.group(1) if match else ""
            self.item.qid = qid
        if not qid:
            raise ValueError("无法从当前回答识别问题 ID")
        try:
            items, is_end = await asyncio.to_thread(
                self.client.question_answers, qid, self._ans_offset
            )
            known = {answer.aid for answer in self._answers}
            self._answers.extend(answer for answer in items if answer.aid not in known)
            self._ans_offset += len(items)
            self._ans_end = is_end
            if self.item.aid:
                for i, it in enumerate(self._answers):
                    if it.aid == self.item.aid:
                        self._ans_idx = i
                        break
            self._ans_ready = True
        except Exception:
            # A transient API failure must remain retryable. Previously it was
            # swallowed after _ans_ready had already been set, permanently
            # turning the failure into a false "no other answers" result.
            self._ans_ready = False
            raise

    async def _switch_answer(self, direction: int):
        if self._ans_switching:
            return
        self._ans_switching = True
        try:
            await self._ensure_answers()
            if not self._answers:
                self.notify("该问题暂无其他回答", timeout=3)
                return
            if direction > 0:
                if self._ans_idx < 0:
                    self._ans_idx = 0
                elif self._ans_idx + 1 >= len(self._answers):
                    if not self._ans_end:
                        items, is_end = await asyncio.to_thread(
                            self.client.question_answers,
                            self.item.qid,
                            self._ans_offset,
                        )
                        self._answers.extend(items)
                        self._ans_offset += len(items)
                        self._ans_end = is_end or not items
                    if self._ans_idx + 1 >= len(self._answers):
                        self.notify("已经是最后一个回答", timeout=3)
                        return
                    self._ans_idx += 1
                else:
                    self._ans_idx += 1
            else:
                if self._ans_idx < 0:
                    # The current answer may be far beyond our bounded lookup
                    # window in the default ranking. The loaded items are still
                    # real alternative answers; allow switching to one of them.
                    self._ans_idx = len(self._answers) - 1
                elif self._ans_idx == 0:
                    self.notify("已经是第一个回答", timeout=3)
                    return
                else:
                    self._ans_idx -= 1

            item = self._answers[self._ans_idx]
            await self._render_item(item)
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=8)
        except Exception as e:
            self.notify(f"切换回答失败：{e}", severity="error", timeout=8)
        finally:
            self._ans_switching = False

    def check_bottom(self):
        """Small toast when the answer content reaches its end."""
        if self._bottom_notified:
            return
        content = self.query_one("#content")
        if content.max_scroll_y is None:
            return
        if content.scroll_y >= content.max_scroll_y - 1:
            self._bottom_notified = True
            hint = "已到回答底部"
            if self.item.kind == "answer":
                hint += " · n 下一个回答 · N 上一个 · c 评论"
            self.notify(hint, timeout=4)

    # -- actions ----------------------------------------------------------

    def action_back(self):
        if _close_page_search(self):
            return
        if self._cursor_mode:
            self._exit_cursor_mode()
            return
        if getattr(self.app, "_select_mode", False):
            self.app.action_select_mode()
            return
        self._leave_screen()

    def action_page_search(self):
        _open_page_search(self)

    def _page_find(self, direction: int):
        box = self.query_one("#content")
        widgets = [widget for widget in box.query(Static) if not isinstance(widget, ImageSection)]
        _find_in_widgets(self, widgets, box, direction)

    @on(Input.Submitted, "#page-search-input")
    def _content_search_submit(self, event: Input.Submitted):
        self._page_find(1)

    @on(Button.Pressed, ".page-search-up")
    def _content_search_up(self, event: Button.Pressed):
        self._page_find(-1)

    @on(Button.Pressed, ".page-search-down")
    def _content_search_down(self, event: Button.Pressed):
        self._page_find(1)

    @on(Button.Pressed, ".page-search-close")
    def _content_search_close(self, event: Button.Pressed):
        _close_page_search(self)

    def _leave_screen(self):
        self._exit_cursor_mode()
        self.app.pop_screen()

    # -- cursor mode ------------------------------------------------------

    def _targets(self) -> list:
        """Focusable targets in document order: image buttons, paragraphs
        that contain links, link buttons, and the open-web button."""
        content = self.query_one("#content")
        targets: list = []
        for sec in self.img_sections:
            targets.append(sec._btn)
            if sec._collapse_btn.display:
                targets.append(sec._collapse_btn)
        for w in content.query(ClickableText):
            if w._links:
                targets.append(w)
        for w in content.query(LinkButton):
            targets.append(w)
        for w in content.query(".tablebtn"):
            targets.append(w)
        ow = content.query("#open-web")
        if ow:
            targets.append(ow.first())
        return targets

    def _focus_target(self, delta: int):
        targets = self._targets()
        if not targets:
            return
        current = self.app.focused
        idx = targets.index(current) if current in targets else -1
        idx = (idx + delta) % len(targets)
        t = targets[idx]
        t.focus()
        content = self.query_one("#content")
        content.scroll_to_widget(t, animate=False, immediate=True)

    def _enter_cursor_mode(self):
        self._cursor_mode = True
        self.app.set_cursor_mode_active(True)
        self._focus_target(1)

    def _exit_cursor_mode(self):
        if self._cursor_mode:
            self._cursor_mode = False
            self.app.set_cursor_mode_active(False)

    def action_cursor_mode(self):
        if self._cursor_mode:
            self._exit_cursor_mode()
        else:
            self._enter_cursor_mode()

    def action_cursor_down(self):
        if self._cursor_mode:
            self._focus_target(1)
        else:
            self.query_one("#content").scroll_down(animate=False, immediate=True)

    def action_cursor_up(self):
        if self._cursor_mode:
            self._focus_target(-1)
        else:
            self.query_one("#content").scroll_up(animate=False, immediate=True)

    def action_cursor_left(self):
        if self.query_one(PageSearchBar).display:
            self._page_find(-1)
            return
        if self._cursor_mode:
            self._focus_target(-1)

    def action_cursor_right(self):
        if self.query_one(PageSearchBar).display:
            self._page_find(1)
            return
        if self._cursor_mode:
            self._focus_target(1)

    def action_cursor_activate(self):
        if not self._cursor_mode:
            return
        w = self.app.focused
        if w is None:
            return
        if isinstance(w, Button):
            w.press()
        elif isinstance(w, ClickableText):
            w.activate()

    def action_page_down(self):
        content = self.query_one("#content")
        content.scroll_to(
            y=content.scroll_y + content.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )

    def action_next_answer(self):
        if self.item.kind != "answer":
            self.notify("此内容不是回答，无法切换", timeout=3)
            return
        self.run_worker(
            self._switch_answer(1), name="switch-ans", group="switch", exclusive=True
        )

    def action_prev_answer(self):
        if self.item.kind != "answer":
            self.notify("此内容不是回答，无法切换", timeout=3)
            return
        self.run_worker(
            self._switch_answer(-1), name="switch-ans", group="switch", exclusive=True
        )

    def action_comments(self):
        if self.item.kind != "answer" or not self.item.aid:
            self.notify("仅回答支持查看评论", timeout=3)
            return
        self.app.push_screen(CommentsScreen(self.client, self.item))

    def action_open_browser(self):
        if self.item.url:
            webbrowser.open(self.item.url)

    def action_toggle_all_images(self):
        """i: open the popup viewer for all images of the answer."""
        urls: list[str] = []
        for sec in self.img_sections:
            for u in sec.urls:
                if u not in urls:
                    urls.append(u)
        if not urls:
            self.notify("本回答没有图片", timeout=3)
            return
        if not kitty.enabled:
            self.run_worker(
                self._fallback_open_all(urls), name="img-sys", group="img", exclusive=True
            )
            return
        self.app.push_screen(ImageViewerScreen(self.client, urls, 0))

    async def _fallback_open_all(self, urls: list[str]):
        opened = 0
        for url in urls:
            try:
                await asyncio.to_thread(open_image_with_system, self.client, url)
                opened += 1
            except Exception:
                pass
        if opened:
            self.notify(f"已用系统看图程序打开 {opened} 张图片", timeout=4)
        else:
            self.notify("图片打开失败", severity="error", timeout=6)


# --------------------------------------------------------------------------
# Comments popup
# --------------------------------------------------------------------------


class CommentScroll(VerticalScroll):
    def watch_scroll_y(self, old, new):
        super().watch_scroll_y(old, new)
        s = self.screen
        if hasattr(s, "check_more"):
            s.check_more()


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
            Static(
                f" {ICON_COMMENT} 评论 · {self.item.author or '匿名用户'}{('（' + count + '）') if count else ''}",
                classes="cmt-title",
            ),
            CommentScroll(id="comments"),
            StatusLine(id="status"),
            id="cmt-panel",
        )

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


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


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


class ZhihuApp(App):
    TITLE = "知乎"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        # hidden from the footer (they live in the command palette / key panel)
        Binding("ctrl+c", "quit", "", show=False),
        Binding("v", "select_mode", "选择模式（复制）", show=False),
        Binding("ctrl+o", "open_url", "打开链接", show=False),
        Binding("ctrl+s", "search", "搜索", show=False),
    ]

    # Textual's linux driver enables these mouse modes; we toggle them off
    # in select mode so the terminal's own selection works for copying.
    _MOUSE_ON = "\x1b[?1000h\x1b[?1003h\x1b[?1006h"
    _MOUSE_OFF = "\x1b[?1006l\x1b[?1003l\x1b[?1000l"

    def __init__(self, client: ZhihuSession, user: dict, cell_size=(8, 16),
                 initial_item: FeedItem | None = None):
        super().__init__()
        for custom_theme in CUSTOM_THEMES:
            self.register_theme(custom_theme)
        self.theme = "zhihu-dark"
        self.client = client
        self.user = user or {}
        self.cell_size = cell_size
        # snappier wheel scrolling
        self.scroll_sensitivity_x = 4.0
        self.scroll_sensitivity_y = 4.0
        self._select_mode = False
        self._cursor_active = False
        self._initial_item = initial_item

    # -- mode indicator ---------------------------------------------------

    @property
    def mode(self) -> str:
        """normal | cursor | visual — drives the bottom-bar badge."""
        if self._select_mode:
            return "visual"
        if self._cursor_active:
            return "cursor"
        return "normal"

    def refresh_mode_badges(self):
        for b in self.query(BottomBar):
            b.update_mode()

    def set_cursor_mode_active(self, active: bool):
        self._cursor_active = active
        self.refresh_mode_badges()

    def get_system_commands(self, screen: Screen):
        for command in super().get_system_commands(screen):
            if command.title != "Change theme":
                yield command
        yield SystemCommand(
            "切换主题",
            "选择内置主题或在 CUSTOM_THEMES 中注册的自定义主题",
            self.action_change_theme,
        )
        enabled = bool(config.get("expand_images_by_default", False))
        state = "开" if enabled else "关"
        yield SystemCommand(
            f"默认展开所有图片：{state}",
            "切换回答正文中的图片是否在进入页面后自动展开",
            self.action_toggle_default_images,
        )
        auto_refresh = bool(config.get("auto_refresh_recommend", False))
        yield SystemCommand(
            f"自动刷新推荐：{'开' if auto_refresh else '关'}",
            "开启后每次切回推荐页都会重新请求；关闭时恢复页面缓存",
            self.action_toggle_auto_refresh_recommend,
        )
        history_enabled = bool(config.get("history_enabled", True))
        yield SystemCommand(
            f"浏览历史：{'开' if history_enabled else '关'}",
            "控制是否记录之后浏览的回答",
            self.action_toggle_history,
        )
        persistent = bool(config.get("history_persistent", False))
        yield SystemCommand(
            f"历史缓存位置：{'配置目录' if persistent else '临时目录 /tmp'}",
            "在临时缓存和持久配置目录之间切换",
            self.action_toggle_history_location,
        )
        yield SystemCommand("清空当前历史记录", str(history.path()), self.action_clear_history)

    def action_toggle_default_images(self):
        enabled = not bool(config.get("expand_images_by_default", False))
        try:
            config.set_value("expand_images_by_default", enabled)
        except Exception as e:
            self.notify(f"保存设置失败：{e}", severity="error", timeout=6)
            return
        self.notify(f"默认展开所有图片：{'开' if enabled else '关'}", timeout=3)

    def action_toggle_auto_refresh_recommend(self):
        enabled = not bool(config.get("auto_refresh_recommend", False))
        try:
            config.set_value("auto_refresh_recommend", enabled)
        except Exception as exc:
            self.notify(f"保存设置失败：{exc}", severity="error", timeout=6)
            return
        self.notify(f"自动刷新推荐：{'开' if enabled else '关'}", timeout=3)

    def action_toggle_history(self):
        enabled = not bool(config.get("history_enabled", True))
        config.set_value("history_enabled", enabled)
        self.notify(f"浏览历史：{'开' if enabled else '关'}", timeout=3)

    def action_toggle_history_location(self):
        persistent = not bool(config.get("history_persistent", False))
        config.set_value("history_persistent", persistent)
        self.notify(f"历史缓存已切换至：{history.path()}", timeout=5)

    def action_clear_history(self):
        history.clear()
        self.notify("已清空当前历史记录", timeout=3)

    # -- search (Ctrl+S) ----------------------------------------------------

    def action_search(self):
        self.run_worker(
            self._search_flow(), name="search-flow", group="open", exclusive=True
        )

    async def _search_flow(self):
        q = await self.push_screen_wait(SearchPromptScreen())
        if not q:
            return
        self.push_screen(SearchScreen(self.client, q, self.user))

    # -- open a specific page (Ctrl+O) ------------------------------------

    def action_open_url(self):
        self.run_worker(
            self._open_url_flow(), name="open-url", group="open", exclusive=True
        )

    async def _open_url_flow(self):
        url = await self.push_screen_wait(UrlPromptScreen())
        if not url:
            return
        self.notify("正在打开链接…", timeout=2)
        try:
            item = await asyncio.to_thread(self.client.fetch_url, url)
        except NotLoggedIn as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        except Exception as e:
            self.notify(f"打开链接失败：{e}", severity="error", timeout=6)
            return
        self.push_screen(ContentScreen(self.client, item))

    # -- select mode ------------------------------------------------------

    def action_select_mode(self):
        self._select_mode = not self._select_mode
        try:
            self._driver.write(self._MOUSE_OFF if self._select_mode else self._MOUSE_ON)
        except Exception:
            pass
        self.refresh_mode_badges()

    # -- crash logging ----------------------------------------------------

    def _log_error(self, title: str, detail: str):
        try:
            log_dir = Path("log")
            log_dir.mkdir(exist_ok=True)
            with open(log_dir / "error.log", "a", encoding="utf-8") as f:
                f.write(
                    f"\n{'=' * 60}\n"
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} {title}\n"
                    f"{detail}\n"
                )
        except Exception:
            pass

    def panic(self, *renderables):
        try:
            import io

            from rich.console import Console

            buf = io.StringIO()
            Console(file=buf, force_terminal=False, width=100).print(*renderables)
            self._log_error("panic", buf.getvalue())
        except Exception:
            pass
        super().panic(*renderables)

    def _handle_exception(self, error: Exception) -> None:
        self._log_error(
            "unhandled exception", traceback.format_exc(limit=30)
        )
        super()._handle_exception(error)

    def on_mount(self):
        if self._initial_item is not None:
            push_item_screen(self, self.client, self._initial_item)
        else:
            self.push_screen(FeedScreen(self.client, self.user))
