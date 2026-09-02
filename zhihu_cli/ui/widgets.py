"""Reusable widgets and helpers (top bars, cards, bottom bar, scroll areas)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import webbrowser

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from ..net import FeedItem, ZhihuSession, fmt_num, fmt_time
from .constants import (
    BADGE_COLOR,
    ICON_COMMENT,
    ICON_IMAGE,
    ICON_LINK,
    ICON_PLAY,
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


def push_item_screen(app: App, client: ZhihuSession, item: FeedItem) -> None:
    """Route a feed/search card to its type-specific screen."""
    from .question import QuestionScreen
    from .content import ContentScreen

    if item.kind == "question":
        app.push_screen(QuestionScreen(client, item))
    else:
        app.push_screen(ContentScreen(client, item))


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

    def __init__(self, badge: str, detail_toggle: bool = False):
        super().__init__(classes="answer-topbar")
        self.badge = badge
        self.detail_toggle = detail_toggle

    def compose(self) -> ComposeResult:
        yield Button(" 返回 ", id="answer-back")
        yield Static(f"展开 · {self.badge}", classes="answer-topbar-title")
        if self.detail_toggle:
            # trailing-end toggle: ▼ 展开问题详情 / ▲ 收起
            yield Button(" ▼ ", id="question-detail-toggle", classes="answer-topbar-toggle")


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
            t.append(f"  ▲ {fmt_num(item.votes)}", style="#34d399")
        if item.comments:
            t.append(f"  {ICON_COMMENT} {fmt_num(item.comments)}", style="#8a93a3")
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
            t.append(f"  ▲ {fmt_num(it.votes)}", style="#34d399")
        if it.comments:
            t.append(f"  {ICON_COMMENT} {fmt_num(it.comments)}", style="#8a93a3")
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
        variables = self.app.get_css_variables()
        # Number badge background cycles through the theme's preset hues.
        hues = [
            variables.get(key, fallback)
            for key, fallback in (
                ("error", "#f43f5e"), ("warning", "#eab308"),
                ("success", "#16a34a"), ("accent", "#38bdf8"),
                ("primary", "#2563eb"), ("secondary", "#7c3aed"),
            )
        ]
        primary = hues[(self.index - 1) % len(hues)]
        muted = variables.get("text-muted", "#8a93a3")
        created = int(self.answer.get("created") or 0)
        date = time.strftime("%Y-%m-%d", time.localtime(created)) if created else ""
        votes = fmt_num(self.answer.get("votes") or 0)
        comments = fmt_num(self.answer.get("comments") or 0)
        images = int(self.answer.get("images") or 0)
        suffix = f"▲ {votes}  {ICON_COMMENT} {comments}  {ICON_IMAGE} {images}"
        if date:
            suffix += f"  |  {date}"

        # Keep the author and right metadata visible; the excerpt yields its
        # space first and receives a muted ellipsis when truncated.
        prefix_text, prefix_cut = _truncate_cells(prefix, width)
        # Start with an unstyled root Text. Giving the root a background
        # would make every subsequently appended span inherit the badge color.
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
        t.append(f"▲ {votes}", style="#34d399")
        t.append(f"  {ICON_COMMENT} {comments}", style=muted)
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
        variables = self.app.get_css_variables()
        primary = variables.get("primary", "#2563eb")
        border = variables.get("border", primary)
        muted = variables.get("text-muted", "#8a93a3")
        title_width = max(1, width - cell_len(when) - 4)
        shown, cut = _truncate_cells(title, title_width)

        def rule(ch: str) -> Text:
            # 单线两端各留两个空格，颜色与推荐页卡片边框一致
            inner = max(1, width - 4)
            line = f"  {ch * inner}  " if width > 4 else ch * max(1, width)
            return Text(line, style=border)

        t = rule("═")
        t.append("\n◆ ", style=primary)
        t.append(shown, style="bold")
        if cut:
            t.append("…", style=muted)
        occupied = 2 + cell_len(shown) + (1 if cut else 0)
        t.append(" " * max(1, width - occupied - cell_len(when)))
        t.append(when, style=muted)
        t.append("\n")
        t.append_text(rule("─"))
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
        variables = self.app.get_css_variables()
        border = variables.get("border", variables.get("primary", "#2563eb"))
        width = max(1, self.size.width)
        inner = max(1, width - 4)
        line = f"  {'═' * inner}  " if width > 4 else "═" * width
        return Text(line, style=border)


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


class CommentScroll(VerticalScroll):
    def watch_scroll_y(self, old, new):
        super().watch_scroll_y(old, new)
        s = self.screen
        if hasattr(s, "check_more"):
            s.check_more()