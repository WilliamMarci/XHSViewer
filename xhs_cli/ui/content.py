"""Note body screen: photo-first rendering, topic text, links, gallery."""
from __future__ import annotations

import asyncio
import re
import webbrowser

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Input, Rule, Static

from .. import config, history, kitty
from ..net import FeedItem, NotLoggedIn, VerificationRequired, XhsClient
from .comments import CommentsScreen
from .constants import ICON_PLAY
from .image import GallerySection, ImageSection, ImageViewerScreen
from .widgets import (
    BottomBar,
    ClickableText,
    ContentScroll,
    LinkButton,
    NoteHeader,
    NoteTopBar,
    PageSearchBar,
    _close_page_search,
    _find_in_widgets,
    _open_page_search,
    open_image_with_system,
    open_link,
)

# #话题[话题]# | @用户 | URL
# 小红书话题语法：``#话题名[话题]#``（方括号内固定是「话题」标记，展示时只取
# 话题名），也兼容 ``#话题名#``。话题名允许包含空格，因此用非贪婪匹配。
_DESC_RE = re.compile(
    r"(?P<topic>#(?P<tname>[^#\[\]]{1,60}?)(?:\[[^\]]*\])?#)"
    r"|(?P<mention>@[0-9A-Za-z_.\-\u4e00-\u9fff]{1,30})"
    r"|(?P<url>https?://[^\s\u3000]+)"
)


def styled_desc(desc: str) -> tuple[Text, list[tuple[int, int, str]]]:
    """Style #topics#, @mentions and URLs inside a note description."""
    text = Text()
    links: list[tuple[int, int, str]] = []
    pos = 0
    for match in _DESC_RE.finditer(desc or ""):
        if match.start() > pos:
            text.append(desc[pos:match.start()])
        if match.group("topic"):
            name = (match.group("tname") or "").strip()
            text.append(f"#{name}", style="bold #ff2442")
        elif match.group("mention"):
            text.append(match.group("mention"), style="#ff8fa3")
        else:
            url = match.group("url") or ""
            start = len(text.plain)
            text.append(url, style="underline #ff8fa3")
            links.append((start, start + len(url), url))
        pos = match.end()
    if pos < len(desc or ""):
        text.append(desc[pos:])
    return text, links


class ContentScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "返回", show=False),
        Binding("j,down", "cursor_down", "", show=False),
        Binding("k,up", "cursor_up", "", show=False),
        Binding("h,left", "cursor_left", "", show=False),
        Binding("l,right", "cursor_right", "", show=False),
        Binding("space", "cursor_activate", "打开", show=False),
        Binding("enter", "cursor_activate", "打开", show=False),
        Binding("n", "next_note", "下一条"),
        Binding("N", "prev_note", "上一条"),
        Binding("comma", "prev_image", "上一张", show=False),
        Binding("full_stop", "next_image", "下一张", show=False),
        Binding("left_square_bracket", "prev_image", "", show=False),
        Binding("right_square_bracket", "next_image", "", show=False),
        Binding("c", "comments", "评论"),
        Binding("o", "open_browser", "浏览器"),
        Binding("i", "cursor_mode", "光标模式", show=False),
        Binding("a", "toggle_all_images", "全部图片", show=False),
        Binding("/", "page_search", "", show=False),
    ]

    def __init__(
        self,
        client: XhsClient,
        item: FeedItem,
        siblings: list[FeedItem] | None = None,
        index: int | None = None,
    ):
        super().__init__()
        self.client = client
        self.item = item
        self._siblings = list(siblings) if siblings else []
        self._sibling_index = index if index is not None else -1
        self._gallery: GallerySection | None = None
        self._rendering = False
        self._bottom_notified = False
        self._cursor_mode = False
        self._switching = False

    def compose(self) -> ComposeResult:
        yield NoteTopBar(f"笔记 · {self.item.badge}")
        yield NoteHeader(id="note-header")
        yield ContentScroll(id="content")
        yield PageSearchBar(id="page-search")
        yield BottomBar(id="bottombar")

    def on_mount(self):
        self.run_worker(self._build(), name="content-build")

    def on_unmount(self):
        if self._cursor_mode:
            self.app.set_cursor_mode_active(False)

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

    # -- content ----------------------------------------------------------

    async def _build(self):
        await self._render_item(self.item)

    async def _hydrate(self, item: FeedItem) -> FeedItem:
        """Feed cards are summaries; fetch the full note (desc + images)."""
        if not item.rid or (item.content and item.images):
            return item
        try:
            full = await asyncio.to_thread(
                self.client.read, item.rid, item.xsec_token
            )
            full.rank = item.rank or full.rank
            full.search_match = item.search_match
            return full
        except NotLoggedIn:
            raise
        except Exception as e:
            self.notify(f"笔记正文加载失败：{e}", severity="warning", timeout=5)
            return item

    async def _render_item(self, item: FeedItem):
        while self._rendering:
            await asyncio.sleep(0.05)
        self._rendering = True
        self._busy("加载中…")
        try:
            try:
                item = await self._hydrate(item)
                await self._render_body(item)
            except VerificationRequired as e:
                self.app.show_verification_required(str(e))
            except NotLoggedIn as e:
                self.notify(str(e), severity="error", timeout=8)
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
        if 0 <= self._sibling_index < len(self._siblings):
            self._siblings[self._sibling_index] = item
        try:
            await asyncio.to_thread(history.record, item)
        except Exception:
            pass
        self._bottom_notified = False
        self.query_one("#note-header", NoteHeader).set_item(item)
        content = self.query_one("#content")
        self._gallery = None
        await content.remove_children()
        content.mount(Rule(classes="rule"))

        it = item

        def mount_gallery():
            # 图集：图片社区的主角
            if it.images:
                self._gallery = GallerySection(
                    self.client, it.images, caption=it.title, title="图集"
                )
                content.mount(self._gallery)
            elif it.thumb:
                content.mount(
                    ImageSection(self.client, [it.thumb], 1, "视频封面")
                )

        def mount_video():
            if it.kind == "video":
                note = f"{ICON_PLAY} 视频笔记，在浏览器中播放"
                if it.duration:
                    note += f"（{it.duration}s）"
                content.mount(Static(Text(note, style="italic #c084fc")))
                play = Button(
                    f" {ICON_PLAY} 播放视频 ", id="open-video", classes="playbtn"
                )
                content.mount(Horizontal(play, classes="row"))

        def mount_tags():
            if it.tags:
                content.mount(
                    Horizontal(
                        *[self._tag_button(name) for name in it.tags[:8]],
                        classes="tag-row",
                    )
                )

        def mount_text():
            if it.content:
                text, links = styled_desc(it.content)
                if links:
                    self._mount_link_buttons(content, links, text)
                content.mount(ClickableText(text, links=links))

        if config.get("note_photo_first", True):
            # Photo first: 图片集在前，文字在后
            for step in (mount_gallery, mount_video, mount_tags, mount_text):
                step()
        else:
            for step in (mount_tags, mount_text, mount_video, mount_gallery):
                step()

        if not it.content and not it.images and not it.thumb:
            content.mount(Static(Text("（无正文内容）", style="dim")))

        # 尾端按钮：可在设置中配置为 在浏览器中打开 / 下一条笔记 / 隐藏
        end_button = config.get("content_end_button", "browser")
        if end_button == "browser":
            content.mount(Horizontal(Button(" 在浏览器打开 ", id="open-web"), classes="row"))
        elif end_button == "next_note":
            content.mount(
                Horizontal(
                    Button(" 下一条笔记 ", id="end-next-note"), classes="row"
                )
            )
        content.scroll_home(animate=False, immediate=True)

    def _tag_button(self, name: str) -> Button:
        button = Button(f"#{name}", classes="tagbtn")
        button._tag = name
        return button

    def _mount_link_buttons(self, content, links, text_obj: Text) -> None:
        urls: list[str] = []
        for _s, _e, url in links:
            if url and url not in urls:
                urls.append(url)
        for url in urls:
            content.mount(LinkButton(url))

    @on(Button.Pressed, "#open-web")
    def _open_web(self, event: Button.Pressed):
        if self.item.url:
            webbrowser.open(self.item.url)

    @on(Button.Pressed, "#end-next-note")
    def _end_next_note(self, event: Button.Pressed):
        self.action_next_note()

    @on(Button.Pressed, "#note-back")
    def _note_back(self, event: Button.Pressed):
        self._leave_screen()

    @on(Button.Pressed, "#open-video")
    def _open_video(self, event: Button.Pressed):
        url = self.item.video_url or self.item.url
        if url:
            webbrowser.open(url)
        else:
            self.notify("未找到视频地址，可在浏览器中打开笔记", timeout=4)

    @on(Button.Pressed, ".linkbtn")
    def _open_link(self, event: Button.Pressed):
        url = getattr(event.button, "url", "")
        if url:
            open_link(self.app, self.client, url)

    @on(Button.Pressed, ".tagbtn")
    def _open_tag(self, event: Button.Pressed):
        tag = getattr(event.button, "_tag", "")
        if not tag:
            return
        from .feed import SearchScreen

        self.app.push_screen(
            SearchScreen(self.client, tag, getattr(self.app, "user", {}))
        )

    # -- note switching (feed siblings) -----------------------------------

    async def _switch_note(self, direction: int):
        if self._switching:
            return
        if not self._siblings:
            self.notify("没有可切换的笔记，请从列表进入", timeout=3)
            return
        next_index = self._sibling_index + direction
        if next_index < 0:
            self.notify("已经是第一条笔记", timeout=3)
            return
        if next_index >= len(self._siblings):
            self.notify("已经是最后一条笔记", timeout=3)
            return
        self._switching = True
        try:
            self._sibling_index = next_index
            await self._render_item(self._siblings[next_index])
        finally:
            self._switching = False

    def action_next_note(self):
        self.run_worker(
            self._switch_note(1), name="switch-note", group="switch", exclusive=True
        )

    def action_prev_note(self):
        self.run_worker(
            self._switch_note(-1), name="switch-note", group="switch", exclusive=True
        )

    # -- gallery ----------------------------------------------------------

    def action_prev_image(self):
        if self._gallery is not None:
            self._gallery.step(-1)

    def action_next_image(self):
        if self._gallery is not None:
            self._gallery.step(1)

    def action_toggle_all_images(self):
        """a: open the popup viewer for all images of the note.

        On terminals without the kitty protocol images stay collapsed and
        ``a`` simply opens the current photo with the system viewer.
        """
        urls = list(self.item.images or [])
        if not urls:
            self.notify("本笔记没有图片", timeout=3)
            return
        start = self._gallery.index if self._gallery else 0
        if not kitty.enabled:
            self.run_worker(
                self._fallback_open_current(urls, start), name="img-sys",
                group="img", exclusive=True,
            )
            return
        self.app.push_screen(ImageViewerScreen(self.client, urls, start))

    async def _fallback_open_current(self, urls: list[str], index: int):
        index = max(0, min(index, len(urls) - 1))
        try:
            await asyncio.to_thread(
                open_image_with_system, self.client, urls[index]
            )
            self.notify(
                f"已用系统看图程序打开第 {index + 1}/{len(urls)} 张图片", timeout=4
            )
        except Exception as e:
            self.notify(f"图片打开失败：{e}", severity="error", timeout=6)

    # -- comments / browser ----------------------------------------------

    def action_comments(self):
        if not self.item.rid:
            self.notify("该笔记无法查看评论", timeout=3)
            return
        self.app.push_screen(CommentsScreen(self.client, self.item))

    def action_open_browser(self):
        if self.item.url:
            webbrowser.open(self.item.url)

    def check_bottom(self):
        pass

    def reload_layout(self):
        """Re-render the note (e.g. after the photo-first setting changes)."""
        self.run_worker(
            self._render_item(self.item),
            name="content-reorder",
            group="content",
            exclusive=True,
        )

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
        widgets = [
            widget
            for widget in box.query(Static)
            if not any(isinstance(parent, GallerySection) for parent in widget.ancestors)
        ]
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
        """Focusable targets in document order: gallery controls, links,
        topic buttons and the end buttons."""
        content = self.query_one("#content")
        targets: list = []
        if self._gallery is not None:
            for button in self._gallery.query(Button):
                targets.append(button)
        for widget in content.query(ClickableText):
            if widget._links:
                targets.append(widget)
        for widget in content.query(LinkButton):
            targets.append(widget)
        for widget in content.query(".tagbtn"):
            targets.append(widget)
        for widget in content.query("#open-web"):
            targets.append(widget)
        for widget in content.query("#open-video"):
            targets.append(widget)
        for widget in content.query("#end-next-note"):
            targets.append(widget)
        return targets

    def _focus_target(self, delta: int):
        targets = self._targets()
        if not targets:
            return
        current = self.app.focused
        idx = targets.index(current) if current in targets else -1
        idx = (idx + delta) % len(targets)
        target = targets[idx]
        target.focus()
        content = self.query_one("#content")
        content.scroll_to_widget(target, animate=False, immediate=True)

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
        if self._gallery is not None:
            self._gallery.step(-1)
            return
        if self._cursor_mode:
            self._focus_target(-1)

    def action_cursor_right(self):
        if self.query_one(PageSearchBar).display:
            self._page_find(1)
            return
        if self._gallery is not None:
            self._gallery.step(1)
            return
        if self._cursor_mode:
            self._focus_target(1)

    def action_cursor_activate(self):
        """Enter/Space: activate the focused actionable widget."""
        widget = self.app.focused
        if widget is None:
            return
        if isinstance(widget, Button):
            widget.press()
        elif isinstance(widget, ClickableText):
            widget.activate()

    def action_page_down(self):
        content = self.query_one("#content")
        content.scroll_to(
            y=content.scroll_y + content.scrollable_content_region.height,
            animate=False,
            immediate=True,
        )
