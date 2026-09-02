import os
import re
import sys
import json
import shutil
import tempfile
import subprocess
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Header, Footer, Static, Input
from textual.screen import ModalScreen
from textual.binding import Binding

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

COOKIE_FILE = "cookies.txt"


def load_netscape_cookies(session: requests.Session, filename: str):
    if not os.path.exists(filename):
        return

    with open(filename, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            domain, flag, path, secure, expires, name, value = parts
            session.cookies.set(
                name,
                value,
                domain=domain,
                path=path,
                secure=(secure.upper() == "TRUE"),
            )


def clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_json_blob(html: str):
    m = re.search(r'<script id="js-initialData" type="text/json">(.*?)</script>', html, re.S)
    if m:
        raw = m.group(1)
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def html_to_text_with_images(html: str):
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    image_entries = []
    img_index = 1

    for img in soup.find_all("img"):
        src = (
            img.get("data-original")
            or img.get("data-actualsrc")
            or img.get("src")
            or ""
        ).strip()
        alt = (img.get("alt") or "").strip()
        placeholder = f"\n[图片#{img_index} 已折叠"
        if alt:
            placeholder += f" alt={alt}"
        placeholder += "]\n"
        img.replace_with(placeholder)
        image_entries.append({"index": img_index, "url": src, "alt": alt})
        img_index += 1

    text = soup.get_text("\n")
    text = clean_text(text)
    return text, image_entries


def parse_zhihu_page(html: str, url: str):
    soup = BeautifulSoup(html, "lxml")
    title = ""

    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    article = soup.find("article")
    if article:
        body_html = str(article)
        text, images = html_to_text_with_images(body_html)
        return title, text, images

    main = soup.find("main")
    if main:
        text, images = html_to_text_with_images(str(main))
        return title, text, images

    # fallback
    text, images = html_to_text_with_images(html)
    return title, text, images


def download_image(session: requests.Session, url: str) -> str | None:
    if not url:
        return None

    try:
        resp = session.get(url, timeout=20, stream=True)
        resp.raise_for_status()

        suffix = ".img"
        path_part = urlparse(url).path.lower()
        for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
            if path_part.endswith(ext):
                suffix = ext
                break

        fd, tmpfile = tempfile.mkstemp(prefix="zhihu_img_", suffix=suffix)
        os.close(fd)
        with open(tmpfile, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return tmpfile
    except Exception:
        return None


def render_image_in_terminal(filepath: str) -> str:
    # 优先 kitten icat
    kitten = shutil.which("kitten")
    if kitten:
        try:
            result = subprocess.run(
                [kitten, "icat", "--unicode-placeholder=0", filepath],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout if result.stdout.strip() else f"[已显示图片] {filepath}"
        except Exception as e:
            return f"[图片显示失败] {e}"

    # 降级到 chafa
    chafa = shutil.which("chafa")
    if chafa:
        try:
            result = subprocess.run(
                [chafa, filepath],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout
        except Exception as e:
            return f"[图片文本预览失败] {e}"

    return f"[无可用图片显示器] 请安装 kitten 或 chafa。文件: {filepath}"


class UrlInputScreen(ModalScreen[str | None]):
    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("输入知乎 URL："),
            Input(placeholder="https://www.zhihu.com/question/... 或 answer/..."),
        )

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)


class ContentView(Static):
    def __init__(self):
        super().__init__("", expand=True)
        self.url = ""
        self.title = ""
        self.raw_text = ""
        self.images = []
        self.images_expanded = False
        self.image_renders = {}

    def build_content(self) -> str:
        lines = []

        if self.title:
            lines.append(f"# {self.title}")
            lines.append("")

        if self.url:
            lines.append(f"URL: {self.url}")
            lines.append("")

        text = self.raw_text or ""
        lines.append(text)

        if self.images:
            lines.append("")
            lines.append("----")
            lines.append(f"图片数量: {len(self.images)}")
            lines.append("按 i 展开/折叠图片")
            lines.append("")

            if self.images_expanded:
                for img in self.images:
                    idx = img["index"]
                    lines.append(f"[图片#{idx}] {img['url']}")
                    rendered = self.image_renders.get(idx)
                    if rendered:
                        lines.append(rendered)
                    else:
                        lines.append("[未渲染]")
                    lines.append("")
            else:
                for img in self.images:
                    lines.append(f"[图片#{img['index']} 已折叠] {img['url']}")

        return "\n".join(lines)

    def refresh_content(self):
        self.update(self.build_content())


class ZhihuTuiApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #content {
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("u", "open_url", "打开URL"),
        Binding("r", "reload_page", "刷新"),
        Binding("i", "toggle_images", "图片折叠/展开"),
        Binding("j", "scroll_down", "下滚"),
        Binding("k", "scroll_up", "上滚"),
        Binding("down", "scroll_down", "下滚", show=False),
        Binding("up", "scroll_up", "上滚", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        load_netscape_cookies(self.session, COOKIE_FILE)
        self.current_url = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield ContentView(id="content")
        yield Footer()

    def on_mount(self):
        content = self.query_one("#content", ContentView)
        content.update(
            "知乎 TUI 浏览器\n\n"
            "按 u 输入 URL\n"
            "按 i 展开/折叠图片\n"
            "按 q 退出\n\n"
            f"Cookie 文件: {COOKIE_FILE} {'(已找到)' if os.path.exists(COOKIE_FILE) else '(未找到)'}"
        )

    def fetch_page(self, url: str):
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text

    def load_url(self, url: str):
        content = self.query_one("#content", ContentView)
        content.update("加载中，请稍候...")

        try:
            html = self.fetch_page(url)
            title, text, images = parse_zhihu_page(html, url)

            content.url = url
            content.title = title
            content.raw_text = text
            content.images = images
            content.images_expanded = False
            content.image_renders = {}
            content.refresh_content()

            self.current_url = url
        except Exception as e:
            content.update(f"加载失败：{e}")

    def action_open_url(self):
        self.push_screen(UrlInputScreen(), self._on_url_input)

    def _on_url_input(self, value):
        if value:
            self.load_url(value)

    def action_reload_page(self):
        if self.current_url:
            self.load_url(self.current_url)

    def action_toggle_images(self):
        content = self.query_one("#content", ContentView)
        if not content.images:
            return

        content.images_expanded = not content.images_expanded

        if content.images_expanded:
            content.update("正在下载并渲染图片，请稍候...")
            image_renders = {}
            for img in content.images:
                idx = img["index"]
                img_url = img["url"]
                if not img_url:
                    image_renders[idx] = "[无图片地址]"
                    continue

                local_file = download_image(self.session, img_url)
                if not local_file:
                    image_renders[idx] = f"[下载失败] {img_url}"
                    continue

                rendered = render_image_in_terminal(local_file)
                image_renders[idx] = rendered

            content.image_renders = image_renders

        content.refresh_content()

    def action_scroll_down(self):
        self.query_one("#content", ContentView).scroll_down(5)

    def action_scroll_up(self):
        self.query_one("#content", ContentView).scroll_up(5)


if __name__ == "__main__":
    app = ZhihuTuiApp()
    app.run()

