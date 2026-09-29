"""遮罩颜色主题：8 套精选预设 + 任意自定义颜色（1677 万色）。

用户只选一个"主题色"，程序自动派生整套协调配色：
遮罩底色（主题色加深）、倒计时（主题色提亮）、正文卡片（主题色半透明）、
卡片描边（主题色亮化）、按钮主色/悬停/按下、进度条。全部在本地计算。
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtGui import QColor

# (key, 显示名, 主题色)
THEMES: Dict[str, tuple] = {
    "teal": ("青色（默认）", "#26A69A"),
    "ocean": ("海蓝", "#2196F3"),
    "violet": ("薰衣草紫", "#7E57C2"),
    "rose": ("玫瑰红", "#EC407A"),
    "amber": ("琥珀橙", "#FFA000"),
    "forest": ("森林绿", "#43A047"),
    "sunset": ("落日橙红", "#FF7043"),
    "graphite": ("石墨灰", "#607D8B"),
}
DEFAULT_THEME = "teal"


def _rgba(color: QColor, alpha: int) -> str:
    c = QColor(color)
    c.setAlpha(alpha)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def derive(base: QColor) -> Dict[str, str]:
    """从一个主题色派生遮罩全套配色。"""
    root = QColor(base).darker(340)            # 遮罩底：大幅加深
    countdown = QColor(base).lighter(135)      # 倒计时：提亮
    card = QColor(base)                        # 正文卡片底：半透明
    border = QColor(base).lighter(140)         # 卡片描边：亮化
    btn = QColor(base)
    btn_hover = QColor(base).lighter(115)
    btn_press = QColor(base).darker(125)
    return {
        "root": _rgba(root, 235),
        "title": "#FFFFFF",
        "countdown": countdown.name(),
        "unit": "#B0BEC5",
        "card_bg": _rgba(card, 71),
        "card_border": _rgba(border, 166),
        "btn": btn.name(),
        "btn_hover": btn_hover.name(),
        "btn_press": btn_press.name(),
        "btn2": "#546E7A",
        "btn2_hover": "#607D8B",
        "btn2_press": "#455A64",
        "progress": btn.name(),
        "progress_track": "rgba(255, 255, 255, 0.10)",
    }


def resolve_theme(name: str, custom_color: str = "") -> Dict[str, str]:
    """按配置解析主题：自定义色合法时优先于预设名，非法/为空回退预设。"""
    if custom_color:
        color = QColor(custom_color)
        if color.isValid():
            return derive(color)
    base_hex = THEMES.get(name, THEMES[DEFAULT_THEME])[1]
    return derive(QColor(base_hex))


def is_valid_theme(name: str) -> bool:
    return name in THEMES or name == "custom"
