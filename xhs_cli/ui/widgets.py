"""Reusable widgets and helpers (top bars, note cards, bottom bar, scroll areas)."""
from __future__ import annotations

import asyncio
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

from .. import config, kitty
from ..net import FeedItem, XhsClient, fmt_num, fmt_time
from .constants import (
    BADGE_COLOR,
    ICON_COLLECT,
    ICON_COMMENT,
    ICON_IMAGE,
    ICON_LIKE,
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
    fd, path = tempfile.mkstemp(suffix=ext, prefix="xhs-img-")
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


def push_item_screen(
    app: App,
    client: XhsClient,
    item: FeedItem,
    siblings: list[FeedItem] | None = None,
    index: int | None = None,
) -> None:
    """Open a note in the content screen (optionally linked to its list)."""
    from .content import ContentScreen

    if siblings and index is not None and index < len(siblings):
        siblings = list(siblings)
        siblings[index] = item
    app.push_screen(ContentScreen(client, item, siblings=siblings, index=index))


# 小红书站内链接（笔记详情）；xsec_token 等后缀不影响匹配。
_XHS_LINK_RE = re.compile(
    r"https?://(?:www\.)?xiaohongshu\.com/"
    r"(?:explore|discovery/item)/[\w]+"
    r"|https?://xhslink\.com/[\w/]+"
)


def _open_xhs_inline(app: App, client: XhsClient, url: str) -> None:
    """Fetch an XHS-internal link and open it inline (in-app)."""

    async def _work():
        try:
            item = await asyncio.to_thread(client.fetch_url, url)
        except Exception as e:
            # 拉取失败（笔记被删/参数受限等）时降级为浏览器打开
            webbrowser.open(url)
            app.notify(f"站内打开失败（{e}），已用浏览器打开", severity="warning", timeout=5)
            return
        push_item_screen(app, client, item)

    app.run_worker(_work(), name="open-link", group="link", exclusive=True)


def open_link(app: App, client: XhsClient, url: str) -> None:
    """Open a content link: XHS-internal ones inline, others in browser."""
    if _XHS_LINK_RE.match((url or "").strip()):
        _open_xhs_inline(app, client, url)
    else:
        webbrowser.open(url)


class TopBar(Static):
    def __init__(self, title: str, right: str = ""):
        super().__init__("")
        self._title = title
        self._right = right

    def render(self) -> Text:
        t = Text(f" {self._title} ", style="bold white")
        if self._right:
            t.append(f"  ·  {self._right}", style="#b9a3aa")
        return t


class NoteTopBar(Horizontal):
    """Note header chrome with a left-aligned back button."""

    def __init__(self, title: str):
        super().__init__(classes="note-topbar")
        self.title = title

    def compose(self) -> ComposeResult:
        yield Button(" 返回 ", id="note-back")
        yield Static(self.title, classes="note-topbar-title")


class FeedTopBar(Horizontal):
    """Feed top bar: 推荐/热门/历史 tabs (one-line) + username right-aligned."""

    def __init__(self, user: dict, mode: str = "recommend", **kwargs):
        super().__init__(classes="topbar", **kwargs)
        self.user = user or {}
        self._mode = mode

    def compose(self) -> ComposeResult:
        yield Button(" 推荐 ", id="tab-rec", classes=self._tab_cls("recommend"))
        yield Button(" 热门 ", id="tab-hot", classes=self._tab_cls("hot"))
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
        "cursor": "#ff2442",
        "visual": "#ff6b81",
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
                mode_button.label = " xhs "
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
            variables.get("panel-darken-1", variables.get("panel", "#231920")),
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


class NoteHeader(Vertical):
    """Fixed header: title / author · likes · collects · comments / link.

    Ctrl+click opens the note in the browser.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.item: FeedItem | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="note-title")
        yield Static("", id="note-meta")

    def set_item(self, item: FeedItem):
        self.item = item
        self.query_one("#note-title", Static).update(
            Text(item.title or "（无标题）", style="bold")
        )
        t = Text()
        if item.rank:
            t.append(f" {item.rank} ", style="bold white on #ff2442")
            t.append("  ")
        t.append(f"{item.author or '未知作者'}", style="bold #ffb3c1")
        if item.votes:
            t.append(f"  {ICON_LIKE} {fmt_num(item.votes)}", style="#ff4d6d")
        if item.collects:
            t.append(f"  {ICON_COLLECT} {fmt_num(item.collects)}", style="#fbbf24")
        if item.comments:
            t.append(f"  {ICON_COMMENT} {fmt_num(item.comments)}", style="#b9a3aa")
        if item.created:
            t.append(f"  {fmt_time(item.created)}", style="#b9a3aa")
        if item.ip:
            t.append(f"  IP {item.ip}", style="#b9a3aa")
        if item.kind == "video":
            t.append(f"  {ICON_PLAY} 视频", style="#c084fc")
            if item.duration:
                t.append(f" {item.duration}s", style="#b9a3aa")
        t.append("\n")
        if item.url:
            t.append(f"{ICON_LINK} ", style="dim")
            t.append(item.url, style="underline #ff8fa3")
        self.query_one("#note-meta", Static).update(t)

    def on_click(self, event: Click):
        if event.ctrl and self.item is not None and self.item.url:
            webbrowser.open(self.item.url)


class _CardOpenMixin:
    """Shared click/keyboard opening behaviour for list and grid cards."""

    def _open_item(self):
        self.focus()
        screen = self.screen
        cards = getattr(screen, "_cards", None)
        items = [c.item for c in cards()] if callable(cards) else None
        index = None
        if items:
            try:
                index = items.index(self.item)
            except ValueError:
                pass
        push_item_screen(
            self.app, self.app.client, self.item, siblings=items, index=index
        )

    def on_click(self, event):
        self._open_item()


class Card(_CardOpenMixin, Vertical, can_focus=True):
    """List card: fixed 3:4 cover (optional) on the left + text on the right."""

    def __init__(self, client, item: FeedItem, show_title: bool = True,
                 show_image: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.item = item
        self.show_title = show_title
        self.show_image = bool(show_image) and kitty.enabled and bool(item.thumb)
        self._search_highlight: tuple[int, int] | None = None

    def compose(self) -> ComposeResult:
        if self.show_image:
            from .image import ThumbImage

            yield Horizontal(
                ThumbImage(
                    self.client, self.item.thumb, cols=12, classes="card-thumb"
                ),
                Static(self._body_text(), classes="card-text"),
                classes="card-row",
            )
        else:
            yield Static(self._body_text(), classes="card-text")

    def _body_text(self) -> Text:
        it = self.item
        t = Text()
        # 热门排序徽标（左上角）
        if it.rank:
            rcolor = "#ff2442" if it.rank <= 3 else "#fbbf24" if it.rank <= 10 else "#8a7580"
            t.append(f" {it.rank} ", style=f"bold white on {rcolor}")
            t.append("  ")
        color = BADGE_COLOR.get(it.badge, "#ff2442")
        t.append(f" {it.badge} ", style=f"bold white on {color}")
        if self.show_title:
            t.append("  ")
            t.append(it.title, style="bold")
        t.append("\n")
        if it.excerpt:
            t.append(it.excerpt, style="#c9b6bd")
            t.append("\n")
        t.append(f" {it.author or '未知作者'}", style="bold #ffb3c1")
        if it.votes:
            t.append(f"  {ICON_LIKE} {fmt_num(it.votes)}", style="#ff4d6d")
        if it.collects:
            t.append(f"  {ICON_COLLECT} {fmt_num(it.collects)}", style="#fbbf24")
        if it.comments:
            t.append(f"  {ICON_COMMENT} {fmt_num(it.comments)}", style="#b9a3aa")
        if it.created:
            t.append(f"  {fmt_time(it.created)}", style="#b9a3aa")
        if it.images:
            t.append(f"  {ICON_IMAGE} {len(it.images)}", style="#ff8fa3")
        if it.kind == "video":
            t.append(f"  {ICON_PLAY} 视频", style="#c084fc")
        if self._search_highlight:
            t.stylize("bold black on #fbbf24", *self._search_highlight)
        return t

    def set_search_highlight(self, span: tuple[int, int] | None):
        self._search_highlight = span
        try:
            self.query_one(".card-text", Static).update(self._body_text())
        except Exception:
            pass


_COUNT_SEMAPHORE = asyncio.Semaphore(1)
# 推荐流卡片没有图片数量，聚焦格子时按需补拉详情；限制频率避免触发风控。
_COUNT_FETCH_INTERVAL = 1.5
_COUNT_LAST_FETCH = 0.0
_COUNT_FOCUS_DELAY = 0.8


class GridCard(_CardOpenMixin, Vertical, can_focus=True):
    """Grid cell: 3:4 cover on top, title/meta below, ``n`` badge overlay."""

    def __init__(self, client, item: FeedItem, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.item = item
        self._thumb = None
        self._count_loading = False
        self._count_timer = None

    def compose(self) -> ComposeResult:
        from .image import ThumbImage

        url = self.item.thumb or (self.item.images[0] if self.item.images else "")
        self._thumb = ThumbImage(
            self.client,
            url,
            auto_width=True,
            label=self._count_label(),
            classes="grid-thumb",
        )
        yield self._thumb
        yield Static(self._title_text(), classes="grid-title")
        yield Static(self._meta_text(), classes="grid-meta")

    def _count_label(self) -> str:
        count = len(self.item.images or [])
        return f" {count} " if count > 1 else ""

    def _title_text(self) -> Text:
        title = " ".join((self.item.title or "（无标题）").split())
        text = Text(title, style="bold")
        if self.item.kind == "video":
            text.append(f"  {ICON_PLAY}", style="#c084fc")
        return text

    def _meta_text(self) -> Text:
        it = self.item
        text = Text()
        text.append(f"{it.author or '未知作者'}", style="#ffb3c1")
        if it.votes:
            text.append(f"  {ICON_LIKE} {fmt_num(it.votes)}", style="#ff4d6d")
        if it.created:
            text.append(f"  {fmt_time(it.created)}", style="#b9a3aa")
        return text

    def on_focus(self):
        """Lazily fetch the photo count for feed items (list payloads only
        carry a cover, not the full image list).

        Debounced so quickly moving through cells does not fire an API call
        per cell (which can trigger XHS risk control)."""
        if (
            self.item.kind != "note"
            or self.item.images
            or self._count_loading
            or not self.item.rid
        ):
            return
        if self._count_timer is not None:
            self._count_timer.stop()
        self._count_timer = self.set_timer(
            _COUNT_FOCUS_DELAY, self._start_count_fetch
        )

    def on_blur(self):
        if self._count_timer is not None:
            self._count_timer.stop()
            self._count_timer = None

    def _start_count_fetch(self):
        self._count_timer = None
        if self._count_loading or self.item.images:
            return
        self._count_loading = True
        self.run_worker(self._fetch_count(), name=f"count-{id(self)}")

    async def _fetch_count(self):
        global _COUNT_LAST_FETCH
        try:
            async with _COUNT_SEMAPHORE:
                wait = _COUNT_FETCH_INTERVAL - (time.monotonic() - _COUNT_LAST_FETCH)
                if wait > 0:
                    await asyncio.sleep(wait)
                _COUNT_LAST_FETCH = time.monotonic()
                full = await asyncio.to_thread(
                    self.client.read, self.item.rid, self.item.xsec_token
                )
        except Exception:
            return
        finally:
            self._count_loading = False
        if full.images:
            self.item.images = full.images
            if self._thumb is not None:
                self._thumb.set_label(self._count_label())


# Grid cell presets: target cell width in terminal columns.
GRID_CELL_WIDTHS = {
    "small": 18,
    "medium": 26,
    "large": 34,
    "xlarge": 46,
}
GRID_CELL_ORDER = ("small", "medium", "large", "xlarge")
GRID_CELL_LABELS = {
    "small": "小",
    "medium": "中",
    "large": "大",
    "xlarge": "特大",
}


def grid_cell_size() -> str:
    size = str(config.get("feed_grid_cell", "xlarge") or "xlarge").lower()
    return size if size in GRID_CELL_WIDTHS else "xlarge"


def grid_column_count(width: int) -> int:
    """Columns that fit ``width`` at the configured cell size (responsive)."""
    cell = GRID_CELL_WIDTHS[grid_cell_size()]
    gutter = 1
    available = max(40, width) - 4  # scroll padding + scrollbar
    return max(1, min(8, (available + gutter) // (cell + gutter)))


def build_card(client, item: FeedItem, view: str = "list",
               show_images: bool = False):
    """Create the list card, or a grid cell when the grid view is active."""
    if view == "grid" and kitty.enabled and item.thumb:
        return GridCard(client, item)
    return Card(client, item, show_image=show_images)


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


class HistoryNoteRow(Static, can_focus=True):
    """A single selectable note from local history."""

    def __init__(self, note: dict, index: int, **kwargs):
        super().__init__(**kwargs)
        self.note = note
        self.index = index
        self.url = note.get("url") or ""

    def render(self) -> Text:
        badge = f" {self.index} "
        title = " ".join((self.note.get("title") or "（无标题笔记）").split())
        prefix = badge + f" {title}："
        excerpt = " ".join((self.note.get("excerpt") or "（无摘要）").split())
        width = max(8, self.size.width - 1)
        variables = self.app.get_css_variables()
        # Number badge background cycles through the theme's preset hues.
        hues = [
            variables.get(key, fallback)
            for key, fallback in (
                ("error", "#ef4444"), ("warning", "#fbbf24"),
                ("success", "#34d399"), ("accent", "#ff8fa3"),
                ("primary", "#ff2442"), ("secondary", "#ff6b81"),
            )
        ]
        primary = hues[(self.index - 1) % len(hues)]
        muted = variables.get("text-muted", "#b9a3aa")
        created = int(self.note.get("created") or 0)
        date = time.strftime("%Y-%m-%d", time.localtime(created)) if created else ""
        votes = fmt_num(self.note.get("votes") or 0)
        comments = fmt_num(self.note.get("comments") or 0)
        images = int(self.note.get("images") or 0)
        suffix = f"{ICON_LIKE} {votes}  {ICON_COMMENT} {comments}  {ICON_IMAGE} {images}"
        if self.note.get("kind") == "video":
            suffix = f"{ICON_PLAY} {suffix}"
        if date:
            suffix += f"  |  {date}"

        # Keep the title and right metadata visible; the excerpt yields its
        # space first and receives a muted ellipsis when truncated.
        prefix_text, prefix_cut = _truncate_cells(prefix, width)
        t = Text()
        t.append(badge, style=f"bold white on {primary}")
        t.append(prefix_text[len(badge):], style="bold #ffb3c1")
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
        t.append(f"{ICON_LIKE} {votes}", style="#ff4d6d")
        t.append(f"  {ICON_COMMENT} {comments}", style=muted)
        t.append(f"  {ICON_IMAGE} {images}", style="#ff8fa3")
        if date:
            t.append(f"  |  {date}", style=muted)
        return t

    def on_click(self, event: Click):
        self.focus()
        if hasattr(self.screen, "open_history_note"):
            self.screen.open_history_note(self)


class HistoryHeading(Static):
    def __init__(self, entry: dict, **kwargs):
        super().__init__(**kwargs)
        self.entry = entry

    def render(self) -> Text:
        width = max(1, self.size.width)
        title = self.entry.get("author") or "未知作者"
        when = fmt_time(self.entry.get("viewed"))
        variables = self.app.get_css_variables()
        primary = variables.get("primary", "#ff2442")
        border = variables.get("border", primary)
        muted = variables.get("text-muted", "#b9a3aa")
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
    """Compact history group (one author); adjacent groups share one double rule."""

    def __init__(self, entry: dict, **kwargs):
        super().__init__(**kwargs)
        self.entry = entry

    def compose(self) -> ComposeResult:
        entry = self.entry
        yield HistoryHeading(entry, classes="history-heading")
        notes = entry.get("notes") or []
        if not notes:
            yield Static("（尚未浏览笔记）", classes="history-empty")
        for index, note in enumerate(notes, 1):
            yield HistoryNoteRow(note, index)


class HistoryEndRule(Static):
    def render(self) -> Text:
        variables = self.app.get_css_variables()
        border = variables.get("border", variables.get("primary", "#ff2442"))
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
    # Plain Static blocks can be highlighted by temporarily replacing their
    # Rich Text renderable, then restoring it.
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
            open_link(self.app, self.app.client, url)
            event.stop()

    def activate(self):
        """Cursor-mode activation: open the first link in this block."""
        if self._links:
            open_link(self.app, self.app.client, self._links[0][2])


class LinkButton(Button):
    """Two-line link card: parsed title first, URL second."""

    def __init__(self, url: str, title: str = "", **kwargs):
        clean_title = " ".join((title or "").split()).strip()
        label = Text()
        if clean_title and clean_title != url:
            label.append(f" {ICON_LINK}  {clean_title}", style="#ff8fa3")
            label.append("\n")
            label.append(f"    {url}", style="underline #ff8fa3")
        else:
            label.append(f" {ICON_LINK}  {url}", style="underline #ff8fa3")
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
