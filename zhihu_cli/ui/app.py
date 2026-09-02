"""The application shell: theme registration, global actions, crash logging."""
from __future__ import annotations

import asyncio
import time
import traceback
from pathlib import Path

from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.screen import Screen

from .. import config, history
from ..net import FeedItem, NotLoggedIn, ZhihuSession
from .constants import CUSTOM_THEMES
from .content import ContentScreen
from .feed import FeedScreen, SearchScreen
from .modals import SearchPromptScreen, UrlPromptScreen
from .widgets import BottomBar, push_item_screen


class ZhihuApp(App):
    TITLE = "知乎"
    CSS_PATH = "../styles.tcss"
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
        end_button = config.get("content_end_button", "browser")
        end_labels = {"browser": "在浏览器中打开", "next_answer": "下一个回答", "none": "隐藏"}
        yield SystemCommand(
            f"尾端按钮：{end_labels.get(end_button, end_button)}",
            "正文底部尾端按钮：在浏览器中打开 / 下一个回答 / 隐藏",
            self.action_toggle_end_button,
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

    def action_toggle_end_button(self):
        order = ("browser", "next_answer", "none")
        current = config.get("content_end_button", "browser")
        current = current if current in order else "browser"
        nxt = order[(order.index(current) + 1) % len(order)]
        try:
            config.set_value("content_end_button", nxt)
        except Exception as exc:
            self.notify(f"保存设置失败：{exc}", severity="error", timeout=6)
            return
        labels = {"browser": "在浏览器中打开", "next_answer": "下一个回答", "none": "隐藏"}
        self.notify(f"尾端按钮：{labels.get(nxt, nxt)}", timeout=3)

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