"""抠图与角色包生成：把用户上传的图片处理为透明背景角色包。

- 去背景：四角洪泛填充（适合纯色/简单背景），清除封闭背景口袋，边缘羽化；
- 自动按文件名关键字映射动画状态（眨眼/挥手/跳跃/坐下，其余归入待机）；
- 单图生成 single 型角色包（复用变换动画），多图生成 frames 型角色包；
- 抠图质量自检：不透明占比过低/过高视为失败，抛出 MattingError。

所有处理均在本地完成，不上传用户图片。
"""
from __future__ import annotations

import json
import math
import os
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_APP: Optional[QApplication] = None


def _ensure_app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class MattingError(RuntimeError):
    """抠图失败（背景过于复杂等），由调用方提示用户。"""


# 文件名关键字 → 动画状态
_KEYWORD_STATES = [
    (("blink", "眨眼"), "blink"),
    (("wave", "挥手", "打招呼"), "wave"),
    (("jump", "跳"), "jump"),
    (("sit", "坐"), "sit"),
    (("idle", "待机", "站立"), "idle"),
]
ALL_STATES = ("idle", "wave", "jump", "sit")


def _near(px: Tuple[int, int, int], bg: Tuple[int, int, int],
          tol: float) -> bool:
    return math.dist(px, bg) <= tol


def matte(img: QImage, bg: Tuple[int, int, int], tol: float = 42.0,
          pockets: bool = True, min_pocket: int = 400) -> QImage:
    """去背景：四角洪泛填充 + 封闭背景口袋清除 + 边缘羽化。"""
    _ensure_app()
    w, h = img.width(), img.height()
    pixels: Dict[Tuple[int, int], Tuple[int, int, int]] = {}
    for y in range(h):
        for x in range(w):
            c = img.pixelColor(x, y)
            pixels[(x, y)] = (c.red(), c.green(), c.blue())

    def is_bg(pt: Tuple[int, int]) -> bool:
        return _near(pixels[pt], bg, tol)

    removed = [[False] * w for _ in range(h)]
    seen = [[False] * w for _ in range(h)]
    queue = deque()
    for sx, sy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        pt = (sx, sy)
        if is_bg(pt) and not seen[sy][sx]:
            seen[sy][sx] = True
            queue.append(pt)
    while queue:
        x, y = queue.popleft()
        removed[y][x] = True
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx] \
                    and is_bg((nx, ny)):
                seen[ny][nx] = True
                queue.append((nx, ny))

    if pockets:  # 清除封闭的大块背景口袋（如手臂与身体之间）
        comp_seen = [[False] * w for _ in range(h)]
        for y in range(h):
            for x in range(w):
                if seen[y][x] or comp_seen[y][x] or not is_bg((x, y)):
                    continue
                comp: List[Tuple[int, int]] = []
                queue = deque([(x, y)])
                comp_seen[y][x] = True
                while queue:
                    cx, cy = queue.popleft()
                    comp.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                                   (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h \
                                and not comp_seen[ny][nx] \
                                and not seen[ny][nx] and is_bg((nx, ny)):
                            comp_seen[ny][nx] = True
                            queue.append((nx, ny))
                if len(comp) >= min_pocket:
                    for cx, cy in comp:
                        removed[cy][cx] = True

    out = img.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(h):
        for x in range(w):
            if removed[y][x]:
                out.setPixelColor(x, y, Qt.GlobalColor.transparent)
    for y in range(h):
        for x in range(w):
            if removed[y][x]:
                continue
            if any(0 <= nx < w and 0 <= ny < h and removed[ny][nx]
                   for nx, ny in ((x + 1, y), (x - 1, y),
                                  (x, y + 1), (x, y - 1))):
                c = out.pixelColor(x, y)
                c.setAlpha(int(c.alpha() * 0.55))
                out.setPixelColor(x, y, c)
    return out


def crop(img: QImage, margin: int = 6) -> QImage:
    """裁剪到非透明内容的外接矩形（留边距）。"""
    w, h = img.width(), img.height()
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > 8:
                x0, y0 = min(x0, x), min(y0, y)
                x1, y1 = max(x1, x), max(y1, y)
    if x1 < 0:
        return img
    x0, y0 = max(0, x0 - margin), max(0, y0 - margin)
    x1, y1 = min(w - 1, x1 + margin), min(h - 1, y1 + margin)
    return img.copy(x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def cutout_image(path: Path, max_side: int = 900) -> QImage:
    """单张图片 → 降采样 → 透明背景 + 裁剪；失败抛 MattingError。

    大图先等比降采样（最长边不超过 max_side），把纯 Python 抠图的
    耗时从分钟级压到秒级；生成的是桌宠贴图，900px 精度足够。
    """
    _ensure_app()
    img = QImage(str(path))
    if img.isNull():
        raise MattingError(f"无法读取图片：{path.name}")
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    longest = max(img.width(), img.height())
    if longest > max_side:
        ratio = max_side / longest
        img = img.scaled(int(img.width() * ratio), int(img.height() * ratio),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    w, h = img.width(), img.height()
    if w < 40 or h < 40:
        raise MattingError("图片尺寸过小（至少 40×40）")
    corners = []
    for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        c = img.pixelColor(cx, cy)
        corners.append((c.red(), c.green(), c.blue()))
    bg = corners[0]
    spread = max(math.dist(bg, c) for c in corners)
    if spread > 60:
        raise MattingError("图片四角颜色不一致，无法识别背景")
    cut = crop(matte(img, bg, tol=42.0, pockets=(math.dist(bg, (0, 0, 0)) < 60
                                                 or math.dist(bg, (255,) * 3)
                                                 < 60)))
    opaque = sum(1 for y in range(cut.height())
                 for x in range(cut.width())
                 if cut.pixelColor(x, y).alpha() > 8)
    frac = opaque / max(1, cut.width() * cut.height())
    if frac < 0.08 or frac > 0.95:
        raise MattingError(
            f"背景识别效果不佳（有效内容占比 {frac:.0%}），"
            "请上传背景更简单的图片，或手动调整")
    return cut


def _state_for_file(path: Path) -> str:
    """按文件名关键字映射动画状态，无法识别归入待机。"""
    stem = path.stem.lower()
    for keys, state in _KEYWORD_STATES:
        if any(k in stem for k in keys):
            return state
    return "idle"


def create_custom_pack(paths: List[Path], name: str,
                       packs_root: Path) -> Dict[str, object]:
    """从用户上传的图片生成角色包。

    - 单张图片：single 型角色包（整图复用呼吸/跳跃/摇摆/眨眼变换动画）；
    - 多张图片：按文件名关键字映射到 idle/wave/jump/sit 状态（frames 型）。
    返回 {"dir": 包目录, "name": 显示名, "kind": "single"/"frames"}。
    """
    _ensure_app()
    if not paths:
        raise MattingError("未选择任何图片")
    safe_name = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()
    if not safe_name:
        safe_name = "我的角色"
    folder = f"custom_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = packs_root / folder
    out_dir.mkdir(parents=True, exist_ok=True)

    cutouts: List[Tuple[Path, QImage]] = []
    for path in paths:
        cutouts.append((path, cutout_image(Path(path))))

    manifest: Dict[str, object] = {
        "name": safe_name,
        "author": "用户上传",
        "source": "user-upload",
        "fps": 8,
        "scale": 0.6,
        "sounds": {},
    }
    if len(cutouts) == 1:
        image = cutouts[0][1]
        image.save(str(out_dir / "character.png"))
        manifest["type"] = "single"
        manifest["image"] = "character.png"
        manifest["animations"] = {
            "idle": {"bob": 3, "sway_deg": 0, "period_ms": 2600},
            "wave": {"bob": 2, "sway_deg": 7, "period_ms": 950},
            "jump": {"hop": 30, "hop_period_ms": 640},
            "happy": {"hop": 16, "sway_deg": 9, "period_ms": 760},
            "sit": {"bob": 2, "sway_deg": 0, "period_ms": 2800},
            "blink": {"bob": 2, "sway_deg": 0, "period_ms": 2200},
        }
        kind = "single"
    else:
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        states: Dict[str, List[str]] = {}
        for i, (src, image) in enumerate(cutouts):
            state = _state_for_file(src)
            rel = f"frames/{state}_{i}.png"
            image.save(str(out_dir / rel))
            states.setdefault(state, []).append(rel)
        if "idle" not in states:  # 待机图缺省用第一张上传的图
            first_state = next(iter(states))
            states["idle"] = states[first_state]
        manifest["type"] = "frames"
        manifest["states"] = states
        kind = "frames"

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dir": out_dir, "name": safe_name, "kind": kind}
