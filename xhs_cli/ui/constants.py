"""Shared UI constants: icons, badge colors, custom themes."""
from __future__ import annotations

from textual.theme import Theme

# 图片按钮改用 emoji（NF 的 MDI 码点在部分终端仍显示异常）
ICON_IMAGE = "🖼"          # emoji: frame with picture
ICON_IMAGES = "🖼"         # emoji: frame with picture
ICON_LINK = "\U000f0337"   # nf-md-link (v3)
ICON_PLAY = "\U000f040a"   # nf-md-play (v3)
ICON_COMMENT = "\U000f017a"  # nf-md-comment (v3)
ICON_COPY = "\U0001f5ce"     # 🗎 copy (U+1F5CE)
# 🔍︎ search (U+1F50D + U+FE0E 文本变体，纯色放大镜)
ICON_SEARCH = "\U0001f50d\ufe0e"
ICON_OPEN = "\u23ce"         # ⏎ 回车/换行（打开）
ICON_CANCEL = "\u00d7"       # × 取消
ICON_LIKE = "\u2665"         # ♥ 点赞
ICON_COLLECT = "\u2605"      # ★ 收藏

BADGE_COLOR = {
    "图文": "#ff2442",
    "视频": "#a855f7",
    "笔记": "#ff2442",
}

# Theme extension point. Add Theme(...) objects here; they are registered at
# startup and automatically appear in Textual's theme picker opened from GO.
CUSTOM_THEMES: tuple[Theme, ...] = (
    Theme(
        name="xhs-red",
        primary="#ff2442",
        secondary="#ff6b81",
        accent="#ff8fa3",
        success="#34d399",
        warning="#fbbf24",
        error="#ef4444",
        background="#120f12",
        surface="#1c1519",
        panel="#231920",
        foreground="#f7eef1",
        dark=True,
    ),
)
