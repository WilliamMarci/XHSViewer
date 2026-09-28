"""Entry point: python -m xhs_cli / console script `xhsviewer`."""
from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from pathlib import Path


def _log_crash(title: str, detail: str) -> None:
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


def _excepthook(exc_type, exc, tb) -> None:
    _log_crash(
        "unhandled exception",
        "".join(traceback.format_exception(exc_type, exc, tb)),
    )


def _thread_excepthook(args) -> None:
    _log_crash(
        f"thread exception ({args.thread.name})",
        "".join(
            traceback.format_exception(args.exc_type, args.exc, args.exc_traceback)
        ),
    )


def main(argv: list[str] | None = None) -> int:
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    ap = argparse.ArgumentParser(
        prog="xhsviewer",
        description="小红书 CLI 客户端：推荐流卡片浏览 + 终端内图片查看（Kitty 协议，支持 Ghostty）",
    )
    ap.add_argument("--login", action="store_true", help="强制重新登录（xhs login）")
    ap.add_argument(
        "url", nargs="?", help="直接打开小红书笔记链接（explore / discovery/item）或笔记 ID"
    )
    args = ap.parse_args(argv)

    from . import kitty
    from .net import XhsClient, XhsError, login_flow

    client = XhsClient()
    try:
        user = None if args.login else client.me()
    except XhsError as e:
        print(f"✗ {e}")
        return 1
    if not user:
        print("未登录或登录已过期。")
        try:
            user = login_flow(client)
        except XhsError as e:
            print(f"✗ {e}")
            return 1
        if not user:
            print("✗ 登录失败：请先运行 `xhs login` 完成登录")
            return 1

    # probe terminal for kitty protocol + cell size before Textual starts
    cell, kitty_ok = kitty.probe()
    kitty.enabled = kitty_ok
    if not kitty_ok:
        print(
            "提示：终端不支持 Kitty 图片协议，图片将仅在浏览器中查看。",
            file=sys.stderr,
        )

    from .ui import XhsApp

    print(
        f"欢迎，{user.get('name', '')}  ↑↓ 选择 · Enter 展开 · o 浏览器打开 · q 退出",
        file=sys.stderr,
    )

    initial_item = None
    if args.url:
        try:
            print(f"正在打开链接：{args.url}", file=sys.stderr)
            initial_item = client.fetch_url(args.url)
        except XhsError as e:
            print(f"✗ 打开链接失败：{e}", file=sys.stderr)
            return 1

    XhsApp(client, user, cell_size=cell, initial_item=initial_item).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
