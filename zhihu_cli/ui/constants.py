"""Shared UI constants: icons, badge colors, custom themes."""
from __future__ import annotations

from textual.theme import Theme

# 图片按钮改用 emoji（用户指定；NF 的 MDI 码点在部分终端仍显示异常）
ICON_IMAGE = "🖼"          # emoji: frame with picture
ICON_IMAGES = "🖼"         # emoji: frame with picture
ICON_LINK = "\U000f0337"   # nf-md-link (v3)
ICON_PLAY = "\U000f040a"   # nf-md-play (v3)
ICON_COMMENT = "\U000f017a"  # nf-md-comment (v3)
ICON_COPY = "\U0001f5ce"     # 🗎 copy (U+1F5CE)

BADGE_COLOR = {
    "回答": "#3b82f6",
    "文章": "#10b981",
    "视频": "#f43f5e",
    "提问": "#a78bfa",
    "想法": "#f59e0b",
}

# Theme extension point. Add Theme(...) objects here; they are registered at
# startup and automatically appear in Textual's theme picker opened from GO.
CUSTOM_THEMES: tuple[Theme, ...] = (
    Theme(
        name="zhihu-dark",
        primary="#2563eb",
        secondary="#7c3aed",
        accent="#38bdf8",
        success="#16a34a",
        warning="#eab308",
        error="#f43f5e",
        background="#0f1115",
        surface="#141821",
        panel="#171a21",
        foreground="#e2e8f0",
        dark=True,
    ),
)