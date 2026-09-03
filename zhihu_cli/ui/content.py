"""Answer/article body screen: content rendering, links, images, switching."""
from __future__ import annotations

import asyncio
import re
import webbrowser

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Input, Rule, Static

from .. import config, history, kitty
from ..htmlparse import Block, find_urls, parse_html, table_to_text
from ..net import FeedItem, NotLoggedIn, ZhihuSession
from .comments import CommentsScreen
from .constants import ICON_PLAY
from .image import ImageSection, ImageViewerScreen
from .modals import TableScreen
from .widgets import (
    AnswerHeader,
    AnswerTopBar,
    BottomBar,
    ClickableText,
    CodeHead,
    ContentScroll,
    LinkButton,
    PageSearchBar,
    _close_page_search,
    _find_in_widgets,
    _open_page_search,
    open_image_with_system,
    open_link,
)


class ContentScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "返回", show=False),
        Binding("j,down", "cursor_down", "", show=False),
        Binding("k,up", "cursor_up", "", show=False),
        Binding("h,left", "cursor_left", "", show=False),
        Binding("l,right", "cursor_right", "", show=False),
        Binding("space", "cursor_activate", "打开", show=False),
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
        yield AnswerTopBar(f"展开 · {self.item.badge}")
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
                    content.mount(
                        CodeHead(b.text.plain if b.text else "", b.lang)
                    )
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
        # 尾端按钮：可在设置中配置为 在浏览器中打开 / 下一个回答 / 隐藏
        end_button = config.get("content_end_button", "browser")
        if end_button == "browser":
            content.mount(Horizontal(Button(" 在浏览器打开 ", id="open-web"), classes="row"))
        elif end_button == "next_answer":
            content.mount(
                Horizontal(
                    Button(" 下一个回答 ", id="end-next-answer"), classes="row"
                )
            )
        content.scroll_home(animate=False, immediate=True)
    @on(Button.Pressed, "#open-web")
    def _open_web(self, event: Button.Pressed):
        if self.item.url:
            webbrowser.open(self.item.url)

    @on(Button.Pressed, "#end-next-answer")
    def _end_next_answer(self, event: Button.Pressed):
        self.action_next_answer()

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
            from .question import QuestionScreen

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
            open_link(self.app, self.client, url)

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
        """Small toast when an answer's content reaches its end.

        Disabled: bottom notification is no longer wanted.
        """
        # if self._bottom_notified or self.item.kind != "answer":
        #     return
        # content = self.query_one("#content")
        # if content.max_scroll_y is None:
        #     return
        # if content.scroll_y >= content.max_scroll_y - 1:
        #     self._bottom_notified = True
        #     self.notify("已到回答底部 · n 下一个回答 · N 上一个 · c 评论", timeout=4)
        pass

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
        """Enter/Space: activate the focused actionable widget (open image,
        collapse image, open link, press a button) — works in any mode."""
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