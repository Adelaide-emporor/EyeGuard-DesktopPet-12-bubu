"""从用户提供的原图抠出透明背景 PNG，制作“一二”“布布”角色包。

原则：绝不重绘角色笔画——只做去背景（洪泛填充 + 大面积背景口袋清除）、
裁剪、封装为角色包（manifest.json + 单张透明 PNG）。
动画由核心程序用位移/摆动/呼吸缩放驱动（见 pet_window.py），帧内容不变形。

用法：python _prepare_characters.py [源图目录]
源图默认取 ZCode 会话图片缓存；一二=白底图，布布=黑底图。
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import deque
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE = Path.home() / (".zcode/cli/image-cache/"
                               "sess_5e79646e-86c5-4eee-8485-5ab106323606")

# 每个角色：源图、背景色、容差、抠图策略与动画参数
SPECS = {
    "yier": {
        "source": DEFAULT_CACHE / "image-e245434e94c07ed054f21ecda9d5605b.png",
        "bg": (255, 255, 255),   # 白底
        "tol": 42.0,             # 与背景色的最大色距（欧氏）
        "pockets": False,        # 白底角色内部也是白色 → 只清除与角落连通的背景
        "scale": 0.46,
        "name": "一二",
        "animations": {
            "idle": {"bob": 3, "sway_deg": 0, "period_ms": 2600},
            "wave": {"bob": 2, "sway_deg": 7, "period_ms": 950},
            "jump": {"hop": 36, "hop_period_ms": 640},
            "happy": {"hop": 18, "sway_deg": 9, "period_ms": 760},
            "sit": {"bob": 2, "sway_deg": 0, "period_ms": 2800},
        },
    },
    "bubu": {
        "source": DEFAULT_CACHE / "image-5210dbd18dce3a68a1d661f17242c623.png",
        "bg": (0, 0, 0),         # 黑底
        "tol": 55.0,             # 角色深棕描边距黑色约 95，不会被误删
        "pockets": True,         # 清除封闭的大块背景口袋（如手臂与脸之间）
        "min_pocket": 400,       # 口袋最小面积（像素）；细描边网络不会误删
        "scale": 0.24,
        "name": "布布",
        "animations": {
            "idle": {"bob": 3, "sway_deg": 0, "period_ms": 2600},
            "wave": {"bob": 2, "sway_deg": 6, "period_ms": 950},
            "jump": {"hop": 30, "hop_period_ms": 640},
            "happy": {"hop": 14, "sway_deg": 8, "period_ms": 760},
            "sit": {"bob": 2, "sway_deg": 0, "period_ms": 2800},
        },
    },
}


def _near(px: tuple, bg: tuple, tol: float) -> bool:
    return math.dist(px, bg) <= tol


def matte(img: QImage, bg: tuple, tol: float, pockets: bool,
          min_pocket: int) -> QImage:
    """去背景：四角洪泛填充必删；布布类再清除封闭的大块背景口袋。"""
    w, h = img.width(), img.height()
    pixels = {}
    for y in range(h):
        for x in range(w):
            c = img.pixelColor(x, y)
            pixels[(x, y)] = (c.red(), c.green(), c.blue())

    def is_bg(pt: tuple) -> bool:
        return _near(pixels[pt], bg, tol)

    removed = [[False] * w for _ in range(h)]

    # 1) 四角洪泛：与角落连通的背景一定可删（描边会把角色内部隔开）
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

    # 2) 封闭背景口袋：成片的背景色但与角落不连通（黑底贴纸的手臂缝隙等）
    if pockets:
        comp_seen = [[False] * w for _ in range(h)]
        for y in range(h):
            for x in range(w):
                pt = (x, y)
                if seen[pt[1]][pt[0]] or not is_bg(pt):
                    continue
                comp = []
                queue = deque([pt])
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

    # 3) 写回 alpha；被删区域邻接的保留像素做半透明羽化，弱化硬边
    out = img.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(h):
        for x in range(w):
            if removed[y][x]:
                out.setPixelColor(x, y, Qt.GlobalColor.transparent)
    for y in range(h):
        for x in range(w):
            if removed[y][x]:
                continue
            touch = any(0 <= nx < w and 0 <= ny < h and removed[ny][nx]
                        for nx, ny in ((x + 1, y), (x - 1, y),
                                       (x, y + 1), (x, y - 1)))
            if touch:
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


def build_pack(name: str, spec: dict, source_override: Path | None) -> None:
    source = source_override or Path(spec["source"])
    if not source.exists():
        raise FileNotFoundError(f"找不到源图：{source}")
    img = QImage(str(source))
    if img.isNull():
        raise RuntimeError(f"源图无法读取：{source}")
    cut = crop(matte(img, spec["bg"], spec["tol"],
                     spec.get("pockets", False), spec.get("min_pocket", 400)))
    out_dir = ROOT / "characters" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    image_rel = "character.png"
    if not cut.save(str(out_dir / image_rel)):
        raise RuntimeError("透明 PNG 写入失败")
    manifest = {
        "name": spec["name"],
        "author": "EyeGuard",
        "type": "single",
        "image": image_rel,
        "scale": spec["scale"],
        "animations": spec["animations"],
        "sounds": {},
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"角色包完成: characters/{name}  透明PNG {cut.width()}x{cut.height()}")


if __name__ == "__main__":
    override = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    for pack_name, pack_spec in SPECS.items():
        build_pack(pack_name, pack_spec, override)
    print("全部完成")
