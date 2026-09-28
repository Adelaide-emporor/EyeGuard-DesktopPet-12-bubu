"""角色包系统：核心程序 + 角色包架构的扫描、加载与管理。

角色包是一个包含 manifest.json 的文件夹：
{
  "name": "一二",
  "fps": 8,
  "frame_size": [200, 220],
  "states": {"idle": ["frames/idle_0.png", ...], "wave": [...], ...},
  "sounds": {"stand": "sounds/stand.wav", "sit": "sounds/sit.wav"}
}
扫描目录：程序目录/characters 与 %APPDATA%/EyeGuard/characters（用户自装包）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtGui import QPixmap

from utils import LOG, app_data_dir

DEFAULT_CHARACTER = "yier"
STANDARD_STATES = ("idle", "wave", "jump", "happy", "sit")
# single 型动作默认参数（bob=上下呼吸像素，sway_deg=左右摆动角度，
# hop=跳跃高度像素，period/hop_period=周期毫秒）
DEFAULT_ANIM = {"bob": 2.0, "sway_deg": 0.0, "hop": 0.0,
                "period_ms": 2400, "hop_period_ms": 640}


def character_dirs() -> List[Path]:
    """角色包扫描目录（去重，优先级从高到低）：

    1. 程序目录/characters —— 发布时随 exe 一起分发，可直接替换；
    2. PyInstaller 解包目录/characters —— --add-data 内嵌的内置角色包，
       保证用户只下载单个 exe 也能使用；
    3. %APPDATA%/EyeGuard/characters —— 用户自装角色包。
    """
    dirs: List[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "characters")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(Path(meipass) / "characters")
    else:
        dirs.append(Path(__file__).resolve().parent / "characters")
    dirs.append(app_data_dir() / "characters")
    out: List[Path] = []
    seen = set()
    for d in dirs:
        resolved = d.resolve()
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


@dataclass
class CharacterPack:
    """一个已加载的角色包。

    kind="single"：单张透明 PNG（用户原图抠像），由核心程序用位移/摆动/
    呼吸缩放做动画（画面绝不变形）；kind="frames"：第三方帧序列（逐帧播放）。
    """

    folder: str                # 包文件夹名（配置里存这个）
    name: str                  # 显示名（manifest.name）
    dir: Path
    fps: int = 30
    scale: float = 0.92        # 相对素材原尺寸的显示缩放
    kind: str = "single"       # single | frames
    image: Optional[QPixmap] = None            # single 型整图
    animations: Dict[str, dict] = field(default_factory=dict)
    states: Dict[str, List[QPixmap]] = field(default_factory=dict)
    sounds: Dict[str, Path] = field(default_factory=dict)
    # 手动框选的眨眼区域（归一化 x,y,w,h）与遮罩色（肤色取样）
    eye_rects: List[Tuple[float, float, float, float]] = field(
        default_factory=list)
    eye_colors: List[str] = field(default_factory=list)

    def frames(self, state: str) -> List[QPixmap]:
        """帧序列（frames 型）；缺失动作回退 idle，再回退任意可用动作。"""
        return (self.states.get(state) or self.states.get("idle")
                or next(iter(self.states.values()), []))

    def anim(self, state: str) -> dict:
        """single 型动作参数（带默认值合并）。"""
        merged = dict(DEFAULT_ANIM)
        merged.update(self.animations.get(state) or {})
        return merged

    def duration_ms(self, state: str) -> int:
        """动作完整播放一次的时长。"""
        if self.kind == "single":
            anim = self.anim(state)
            return int(anim.get("hop_period_ms", 640) if state == "jump"
                       else anim.get("period_ms", 2400))
        return int(1000.0 * len(self.frames(state)) / max(1, self.fps))


def _load_pack(dir_path: Path) -> Optional[CharacterPack]:
    """从文件夹加载单个角色包；任何问题都返回 None 并记录日志。"""
    manifest = dir_path / "manifest.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        kind = str(data.get("type", "frames"))
        pack = CharacterPack(
            folder=dir_path.name,
            name=str(data.get("name", dir_path.name)),
            dir=dir_path,
            fps=max(1, int(data.get("fps", 30 if kind == "single" else 8))),
            scale=float(data.get("scale", 0.92)),
            kind=kind,
        )
        if kind == "single":
            image = QPixmap(str(dir_path / str(data.get("image", ""))))
            if image.isNull():
                LOG.warning("角色包 %s 缺少可读的 image 素材", dir_path.name)
                return None
            pack.image = image
            pack.animations = {
                str(state): dict(anim or {})
                for state, anim in (data.get("animations") or {}).items()}
        else:
            for state, files in (data.get("states") or {}).items():
                frames: List[QPixmap] = []
                for rel in files:
                    pixmap = QPixmap(str(dir_path / str(rel)))
                    if not pixmap.isNull():
                        frames.append(pixmap)
                if frames:
                    pack.states[str(state)] = frames
        for key, rel in (data.get("sounds") or {}).items():
            sound = dir_path / str(rel)
            if sound.exists():
                pack.sounds[str(key)] = sound
        for rect in (data.get("eye_rects") or []):
            try:
                rx, ry, rw, rh = (float(v) for v in rect)
                pack.eye_rects.append((rx, ry, rw, rh))
            except (TypeError, ValueError):
                continue
        pack.eye_colors = _eye_cover_colors(
            dir_path / str(data.get("image", "character.png"))
            if pack.kind == "single" else None, pack.eye_rects)
        if pack.kind == "frames":
            if not pack.states:
                LOG.warning("角色包 %s 没有有效帧，已跳过", dir_path.name)
                return None
            missing = [s for s in STANDARD_STATES if s not in pack.states]
            if missing:
                LOG.warning("角色包 %s 缺少动作 %s（播放时回退 idle）",
                            dir_path.name, missing)
        return pack
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.warning("角色包 %s 加载失败: %s", dir_path, exc)
        return None


def _eye_cover_colors(image_path: Optional[Path],
                      rects: List[Tuple[float, float, float, float]]
                      ) -> List[str]:
    """在每个眨眼区域上方取样肤色作为遮罩色；取样失败回退暖肤色。"""
    fallback = "#E8BFA8"
    if not rects or image_path is None or not image_path.exists():
        return [fallback] * len(rects)
    try:
        from PySide6.QtGui import QImage
        img = QImage(str(image_path))
        if img.isNull():
            return [fallback] * len(rects)
        colors: List[str] = []
        for rx, ry, rw, rh in rects:
            samples = []
            top = max(0, int(ry * img.height()) - 3)
            for sx in range(int(rx * img.width()),
                            min(img.width(), int((rx + rw) * img.width())), 2):
                for sy in range(top, min(img.height(), top + 3)):
                    c = img.pixelColor(sx, sy)
                    if c.alpha() > 100:
                        samples.append((c.red(), c.green(), c.blue()))
            if samples:
                r = sum(s[0] for s in samples) // len(samples)
                g = sum(s[1] for s in samples) // len(samples)
                b = sum(s[2] for s in samples) // len(samples)
                colors.append(f"#{r:02X}{g:02X}{b:02X}")
            else:
                colors.append(fallback)
        return colors
    except Exception:  # noqa: BLE001  取样失败不影响角色包加载
        return [fallback] * len(rects)


class CharacterManager:
    """扫描所有角色包目录，按文件夹名提供角色包。"""

    def __init__(self) -> None:
        self._packs: Dict[str, CharacterPack] = {}
        self._scan()
        summary = ", ".join(f"{k}({v.name})" for k, v in self._packs.items())
        if self._packs:
            LOG.info("已加载 %d 个角色包：%s", len(self._packs), summary)
        else:
            LOG.warning("未找到任何角色包：请确认 characters/ 目录中存在"
                        "含 manifest.json 的角色包文件夹")

    def _scan(self) -> None:
        """扫描所有角色包目录。"""
        for base in character_dirs():
            if not base.exists():
                continue
            for child in sorted(base.iterdir()):
                if not child.is_dir() or child.name in self._packs:
                    continue
                pack = _load_pack(child)
                if pack is not None:
                    self._packs[pack.folder] = pack

    def rescan(self) -> None:
        """重新扫描角色包目录（用户新增自定义角色后调用）。"""
        self._packs.clear()
        self._scan()
        summary = ", ".join(f"{k}({v.name})" for k, v in self._packs.items())
        LOG.info("重新扫描角色包：%s", summary or "无")

    def packs(self) -> Dict[str, CharacterPack]:
        """所有已加载角色包，键为文件夹名。"""
        return self._packs

    def names(self) -> List[Tuple[str, str]]:
        """[(文件夹名, 显示名), ...]，用于切换 UI。"""
        return [(key, pack.name) for key, pack in self._packs.items()]

    def get(self, folder: str) -> Optional[CharacterPack]:
        """按文件夹名取包；回退默认包，再回退第一个可用包。"""
        return (self._packs.get(folder) or self._packs.get(DEFAULT_CHARACTER)
                or (next(iter(self._packs.values())) if self._packs else None))
