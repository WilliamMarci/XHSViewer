"""Entry point: python -m zhihu_cli / console script `zhihu`."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="zhihu",
        description="知乎 CLI 客户端：推荐流卡片浏览 + 终端内图片查看（Kitty 协议，支持 Ghostty）",
    )
    ap.add_argument("--login", action="store_true", help="强制重新登录")
    ap.add_argument("--cookies", help="直接提供 Cookie 字符串（形如 d_c0=xxx; z_c0=yyy）")
    args = ap.parse_args(argv)

    from . import kitty
    from .net import (
        ZhihuError,
        ZhihuSession,
        load_cookies,
        login_flow,
        parse_cookie_header,
    )

    if args.cookies:
        cookies = parse_cookie_header(args.cookies)
    elif args.login:
        cookies = {}
    else:
        cookies = load_cookies()

    client = ZhihuSession(cookies)
    user = client.me()
    if not user:
        print("未登录或登录已过期。")
        try:
            cookies = login_flow()
        except ZhihuError as e:
            print(f"✗ {e}")
            return 1
        client = ZhihuSession(cookies)
        user = client.me()
        if not user:
            print("✗ 登录失败：无法验证登录态")
            return 1

    # probe terminal for kitty protocol + cell size before Textual starts
    cell, kitty_ok = kitty.probe()
    kitty.enabled = kitty_ok
    if not kitty_ok:
        print(
            "提示：终端不支持 Kitty 图片协议，图片将仅在浏览器中查看。",
            file=sys.stderr,
        )

    from .ui import ZhihuApp

    print(f"欢迎，{user.get('name', '')}  ↑↓ 选择 · Enter 展开 · o 浏览器打开 · q 退出", file=sys.stderr)
    ZhihuApp(client, user, cell_size=cell).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())