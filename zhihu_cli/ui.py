"""Textual UI: recommendation feed cards + expandable content + inline images."""
from __future__ import annotations

import asyncio
import math
import webbrowser

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Rule, Static

from . import kitty
from .htmlparse import Block, parse_html, table_to_text
from .net import (
    FeedItem,
    NotLoggedIn,
    ZhihuSession,
    fmt_num,
    fmt_time,
)

BADGE_COLOR = {
    "回答": "#3b82f6",
    "文章": "#10b981",
    "视频": "#f43f5e",
    "提问": "#a78bfa",
    "想法": "#f59e0b",
}


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


class StatusLine(Static):
    """Small animated spinner at the bottom-left, shown while loading."""

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._spin = 0
        self._text = ""
        self._active = False

    def on_mount(self):
        self.set_interval(0.12, self._tick)

    def busy(self, text: str = "加载中…"):
        self._text = text
        self._active = True
        self._spin = 0
        self.update(f"{self.SPINNER[0]} {text}")

    def idle(self):
        self._active = False
        self.update("")

    def _tick(self):
        if not self._active:
            return
        self._spin += 1
        self.update(f"{self.SPINNER[self._spin % len(self.SPINNER)]} {self._text}")


# --------------------------------------------------------------------------
# Feed
# --------------------------------------------------------------------------


class Card(Static, can_focus=True):
    def __init__(self, item: FeedItem, **kwargs):
        super().__init__(**kwargs)
        self.item = item

    def render(self) -> Text:
        it = self.item
        t = Text()
        color = BADGE_COLOR.get(it.badge, "#64748b")
        t.append(f" {it.badge} ", style=f"bold white on {color}")
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
            t.append(f"  🖼 {len(it.images)}", style="#38bdf8")
        if it.kind == "zvideo":
            t.append("  ▶", style="#f43f5e")
        return t

    def on_click(self, event):
        self.focus()
        self.app.push_screen(ContentScreen(self.app.client, self.item))


class FeedScroll(VerticalScroll, inherit_bindings=False):
    # Don't let up/down/left/right be consumed by the scroll container:
    # those keys move the card selection (screen bindings).  Page/Home/End
    # still scroll the viewport (instantly).  Mouse wheel is unaffected.
    BINDINGS = [
        Binding("pageup", "instant_page_up", show=False),
        Binding("pagedown", "instant_page_down", show=False),
        Binding("home", "instant_home", show=False),
        Binding("end", "instant_end", show=False),
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
        Binding("j,down", "next_card", "下一条", show=False),
        Binding("k,up", "prev_card", "上一条", show=False),
        Binding("enter", "open_card", "展开"),
        Binding("r", "refresh", "刷新"),
        Binding("o", "open_browser", "浏览器"),
        Binding("g", "go_top", "顶部", show=False),
        Binding("G", "go_bottom", "底部", show=False),
        Binding("q", "quit_app", "退出"),
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

    def compose(self) -> ComposeResult:
        yield TopBar("知乎 · 推荐", self.user.get("name") or "")
        yield FeedScroll(Static("  加载中…", classes="hint"), id="feed")
        yield StatusLine(id="status")
        yield Footer()

    def on_mount(self):
        self.run_worker(
            self._initial_load(), name="feed-initial", group="feed", exclusive=True
        )

    # -- loading ---------------------------------------------------------

    def _busy(self, text: str = "加载中…"):
        status = self.query_one("#status", StatusLine)
        status.busy(text)

    def _idle(self):
        status = self.query_one("#status", StatusLine)
        status.idle()

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
            items, is_end = await asyncio.to_thread(
                self.client.feed, self.after_id, self.page
            )
            feed = self.query_one("#feed")
            cards = [Card(it) for it in items]
            if cards:
                feed.mount(*cards)
            if items:
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
        item = cards[self._focus_index()].item
        self.app.push_screen(ContentScreen(self.client, item))

    def action_open_browser(self):
        cards = self._cards()
        if not cards:
            return
        item = cards[self._focus_index()].item
        if item.url:
            webbrowser.open(item.url)

    def action_refresh(self):
        self.query_one("#feed").remove_children()
        self.after_id = ""
        self.page = 1
        self.is_end = False
        self.focus_index = 0
        self.run_worker(
            self._initial_load(), name="feed-initial", group="feed", exclusive=True
        )

    def action_go_top(self):
        self.query_one("#feed").scroll_home(animate=False, immediate=True)

    def action_go_bottom(self):
        self.query_one("#feed").scroll_end(animate=False, immediate=True)

    def action_quit_app(self):
        self.app.exit()


# --------------------------------------------------------------------------
# Inline images (kitty graphics protocol)
# --------------------------------------------------------------------------


class KittyImage(Static):
    """Renders a PNG in the terminal via the kitty protocol, re-emitting on
    redraw and deleting itself when scrolled out of view."""

    _seq = 0

    def __init__(self, data: bytes, cell: tuple[int, int], max_cols: int, **kwargs):
        super().__init__("", **kwargs)
        self._cell = cell
        KittyImage._seq += 1
        self.img_id = f"z{KittyImage._seq:x}"
        self.png = b""
        self.px_w = 0
        self.px_h = 0
        self.rows = 1
        self._shown = False
        self._error = ""
        cell_w, cell_h = cell
        max_px = max(160, max(60, max_cols or 80) * cell_w)
        try:
            self.png, self.px_w, self.px_h = kitty.prepare_png(data, max_px)
            self.rows = max(1, math.ceil(self.px_h / max(1, cell_h)))
        except Exception as e:
            self._error = str(e)
        # height must be known before mount so layout reserves the space
        self.styles.height = self.rows

    def render(self) -> Text:
        if self._error:
            return Text(f"（图片解析失败：{self._error}）", style="dim")
        return Text("")

    def on_mount(self):
        screen = self.screen
        if hasattr(screen, "register_image"):
            screen.register_image(self)
        # region is not final during mount; sync after layout settles
        self.app.call_after_refresh(self.sync_visibility)

    def on_unmount(self):
        screen = getattr(self, "screen", None)
        if screen is not None and hasattr(screen, "unregister_image"):
            screen.unregister_image(self)
        self._delete()

    def _driver(self):
        return self.app._driver

    def _find_scroll(self):
        w = self.parent
        while w is not None:
            if isinstance(w, VerticalScroll):
                return w
            w = w.parent
        return None

    def _content_top(self) -> float:
        """Widget top edge in the scroll container's (unscrolled) content coords."""
        scroll = self._find_scroll()
        if scroll is None:
            return 0.0
        return self.region.y - scroll.region.y

    def _screen_pos(self):
        scroll = self._find_scroll()
        if scroll is None:
            return None
        return self.region.y - scroll.scroll_y, self.region.x - scroll.scroll_x

    def _emit(self):
        if not self.png or not kitty.enabled:
            return
        pos = self._screen_pos()
        if pos is None:
            return
        row, col = pos
        scroll = self._find_scroll()
        if scroll is None:
            return
        if row < 0 or row >= scroll.region.height:
            return
        try:
            data = f"\x1b[{row + 1};{col + 1}H" + kitty.emit_image(
                self.png, self.img_id, self.px_w, self.px_h
            )
            self._driver().write(data)
        except Exception:
            pass

    def _delete(self):
        if not kitty.enabled:
            return
        try:
            self._driver().write(kitty.delete_image(self.img_id))
        except Exception:
            pass

    def sync_visibility(self):
        if not self.png:
            return
        scroll = self._find_scroll()
        if scroll is None or self.region.height <= 0:
            return
        view_top = scroll.scroll_y
        view_h = scroll.region.height
        my_top = self._content_top()
        my_bottom = my_top + self.rows
        visible = my_bottom >= view_top and my_top <= view_top + view_h
        if visible and not self._shown:
            self._shown = True
            self._emit()
        elif not visible and self._shown:
            self._shown = False
            self._delete()


class ImageSection(Vertical):
    """One image (or group) with its own independent expand button."""

    def __init__(self, client: ZhihuSession, urls: list[str], no: int, caption: str = ""):
        super().__init__(classes="imgsec")
        self.client = client
        self.urls = urls
        self.no = no
        self.caption = caption
        self.expanded = False
        self._images: list[KittyImage] = []
        self._btn: Button | None = None

    def compose(self) -> ComposeResult:
        label = f"🖼 图片 {self.no}"
        if self.caption:
            label += f" · {self.caption[:24]}"
        self._btn = Button(label, classes="imgbtn")
        yield self._btn

    @on(Button.Pressed)
    def _on_pressed(self, event: Button.Pressed):
        if event.button is not self._btn:
            return
        if not kitty.enabled:
            self.app.notify("当前终端不支持内联图片（需 Kitty 协议，如 Ghostty）", severity="warning")
            return
        self.toggle()

    def toggle(self):
        if self.expanded:
            self._collapse()
        else:
            self.run_worker(self._expand(), name=f"img-{id(self)}", group="imgload")

    def _label(self, extra: str = "") -> str:
        base = f"🖼 图片 {self.no}"
        if self.caption:
            base += f" · {self.caption[:24]}"
        return base + extra

    async def _expand(self):
        if self.expanded:
            return
        self._btn.disabled = True
        self._btn.label = "下载中…"
        screen = self.screen
        if hasattr(screen, "_busy"):
            screen._busy("下载图片…")
        datas: list[bytes] = []
        try:
            for u in self.urls:
                datas.append(await asyncio.to_thread(self.client.fetch_image_bytes, u))
        except Exception as e:
            self._btn.disabled = False
            self._btn.label = self._label("（下载失败）")
            self.app.notify(f"图片下载失败：{e}", severity="error", timeout=6)
            if hasattr(screen, "_idle"):
                screen._idle()
            return
        self.expanded = True
        cell = self.app.cell_size
        max_cols = (self.app.size.width or 80) - 2
        for data in datas:
            img = KittyImage(data, cell, max_cols)
            self._images.append(img)
            self.mount(img)
        self._btn.disabled = False
        self._btn.label = self._label("（收起）")
        if hasattr(screen, "_idle"):
            screen._idle()

    def _collapse(self):
        for img in self._images:
            img.remove()
        self._images.clear()
        self.expanded = False
        self._btn.label = self._label()


# --------------------------------------------------------------------------
# Content (expanded) screen
# --------------------------------------------------------------------------


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
        s = self.screen
        if hasattr(s, "sync_images"):
            s.sync_images()

    def watch_scroll_y(self, old, new):
        super().watch_scroll_y(old, new)
        s = self.screen
        if hasattr(s, "sync_images"):
            s.sync_images()
        if hasattr(s, "check_bottom"):
            s.check_bottom()


class ContentScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "返回"),
        Binding("j,down", "scroll_down", None, show=False),
        Binding("k,up", "scroll_up", None, show=False),
        Binding("space", "page_down", "下翻页"),
        Binding("n", "next_answer", "下一个回答"),
        Binding("N", "prev_answer", "上一个回答"),
        Binding("C", "comments", "评论"),
        Binding("o", "open_browser", "浏览器"),
        Binding("i", "toggle_all_images", "全部图片"),
    ]

    def __init__(self, client: ZhihuSession, item: FeedItem):
        super().__init__()
        self.client = client
        self.item = item
        self._images: set[KittyImage] = set()
        self.img_sections: list[ImageSection] = []
        self._rendering = False
        self._bottom_notified = False
        # answer switching state
        self._answers: list[FeedItem] = []
        self._ans_idx: int = -1
        self._ans_offset: int = 0
        self._ans_end: bool = False
        self._ans_ready: bool = False
        self._ans_switching: bool = False

    def compose(self) -> ComposeResult:
        yield TopBar(f"展开 · {self.item.badge}")
        yield ContentScroll(id="content")
        yield StatusLine(id="status")
        yield Footer()

    def on_mount(self):
        self.run_worker(self._build(), name="content-build")

    def _busy(self, text: str = "加载中…"):
        status = self.query_one("#status", StatusLine)
        status.busy(text)

    def _idle(self):
        status = self.query_one("#status", StatusLine)
        status.idle()

    # -- image bookkeeping ------------------------------------------------

    def register_image(self, w: KittyImage):
        self._images.add(w)

    def unregister_image(self, w: KittyImage):
        self._images.discard(w)

    def sync_images(self):
        for w in list(self._images):
            w.sync_visibility()

    def on_resize(self, event):
        for w in list(self._images):
            w._shown = False
            w._delete()
        self.app.call_after_refresh(self.sync_images)

    # -- content ----------------------------------------------------------

    async def _build(self):
        await self._render_item(self.item)

    async def _render_item(self, item: FeedItem):
        while self._rendering:
            await asyncio.sleep(0.05)
        self._rendering = True
        self._busy("加载中…")
        try:
            self.item = item
            self._bottom_notified = False
            content = self.query_one("#content")
            for w in list(self._images):
                w._delete()
            self._images.clear()
            self.img_sections.clear()
            content.remove_children()

            it = item
            content.mount(Static(Text(it.title, style="bold"), classes="title"))
            meta = Text()
            meta.append(f"  {it.author or '匿名用户'}", style="bold #eab308")
            if it.votes:
                meta.append(f"  赞 {fmt_num(it.votes)}", style="#34d399")
            if it.comments:
                meta.append(f"  评 {fmt_num(it.comments)}", style="#8a93a3")
            if it.created:
                meta.append(f"  {fmt_time(it.created)}", style="#8a93a3")
            if it.url:
                meta.append(f"\n  {it.url}", style="dim")
            content.mount(Static(meta, classes="meta"), Rule(classes="rule"))

            def add_img_section(urls, caption=""):
                sec = ImageSection(
                    self.client, urls, len(self.img_sections) + 1, caption
                )
                self.img_sections.append(sec)
                content.mount(sec)

            if it.kind == "zvideo":
                note = "▶ 视频内容请在浏览器中打开"
                if it.video_url:
                    note += "（已找到播放直链）"
                content.mount(Static(Text(note, style="italic #f43f5e")))
                if it.thumb:
                    add_img_section([it.thumb], "视频封面")
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
                    elif b.kind == "list":
                        text = Text()
                        for item_text in b.items:
                            text.append_text(item_text)
                            text.append("\n")
                        content.mount(Static(text))
                    elif b.kind == "table":
                        content.mount(Static(table_to_text(b)))
                    elif b.kind == "hr":
                        content.mount(Static(b.text))
                    else:
                        if b.text:
                            content.mount(Static(b.text))
                if not blocks and not it.images:
                    content.mount(Static("（无正文内容）", style="dim"))

            row = Horizontal(Button(" 在浏览器打开 ", id="open-web"), classes="row")
            content.mount(row)
            content.scroll_home(animate=False, immediate=True)
        finally:
            self._rendering = False
            self._idle()

    @on(Button.Pressed, "#open-web")
    def _open_web(self, event: Button.Pressed):
        if self.item.url:
            webbrowser.open(self.item.url)

    # -- answer switching -------------------------------------------------

    async def _ensure_answers(self):
        """Load the question's answer list (default sort) and locate the
        current answer inside it.  Handles boundary cases: empty list,
        current answer not in the list, list end."""
        if self._ans_ready:
            return
        self._ans_ready = True
        if not self.item.qid:
            return
        try:
            for _ in range(6):
                items, is_end = await asyncio.to_thread(
                    self.client.question_answers, self.item.qid, self._ans_offset
                )
                self._answers.extend(items)
                self._ans_offset += len(items)
                self._ans_end = is_end
                if self.item.aid:
                    for i, it in enumerate(self._answers):
                        if it.aid == self.item.aid:
                            self._ans_idx = i
                            return
                if is_end or not items:
                    return
        except Exception:
            pass
        self._ans_idx = -1

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
                if self._ans_idx <= 0:
                    self.notify("已经是第一个回答", timeout=3)
                    return
                self._ans_idx -= 1

            item = self._answers[self._ans_idx]
            await self._render_item(item)
            self.notify(f"回答 {self._ans_idx + 1} · {item.author or '匿名'}", timeout=3)
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
                hint += " · n 下一个回答 · N 上一个 · C 评论"
            self.notify(hint, timeout=4)

    # -- actions ----------------------------------------------------------

    def action_back(self):
        for w in list(self._images):
            w._delete()
        self.app.pop_screen()

    def action_scroll_down(self):
        self.query_one("#content").scroll_down(animate=False, immediate=True)

    def action_scroll_up(self):
        self.query_one("#content").scroll_up(animate=False, immediate=True)

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
        if not self.img_sections:
            return
        if all(s.expanded for s in self.img_sections):
            for s in self.img_sections:
                s.toggle()
        else:
            for s in self.img_sections:
                if not s.expanded:
                    s.toggle()


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
        Binding("j,down", "scroll_down", None, show=False),
        Binding("k,up", "scroll_up", None, show=False),
        Binding("space", "page_down", None, show=False),
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
                f" 💬 评论 · {self.item.author or '匿名用户'}{('（' + count + '）') if count else ''}",
                classes="cmt-title",
            ),
            CommentScroll(Static(" 加载中…", classes="hint"), id="comments"),
            StatusLine(id="status"),
            id="cmt-panel",
        )

    def on_mount(self):
        self.run_worker(self._load(), name="cmt", group="cmt", exclusive=True)

    def _busy(self, text: str = "加载中…"):
        status = self.query_one("#status", StatusLine)
        status.busy(text)

    def _idle(self):
        status = self.query_one("#status", StatusLine)
        status.idle()

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
        t.append(f" {c.author or '匿名'}", style="bold #eab308")
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


class ZhihuApp(App):
    TITLE = "知乎"
    CSS_PATH = "styles.tcss"
    BINDINGS = [Binding("ctrl+c", "quit", "退出")]

    def __init__(self, client: ZhihuSession, user: dict, cell_size=(8, 16)):
        super().__init__()
        self.client = client
        self.user = user or {}
        self.cell_size = cell_size
        # snappier wheel scrolling
        self.scroll_sensitivity_x = 4.0
        self.scroll_sensitivity_y = 4.0

    def on_mount(self):
        self.push_screen(FeedScreen(self.client, self.user))