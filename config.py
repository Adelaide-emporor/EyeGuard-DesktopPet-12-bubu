"""配置与统计数据模型：JSON 持久化到 %APPDATA%\\EyeGuard。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, Dict

from utils import LOG, is_valid_sound, config_path, stats_path

# 提醒方式常量
MODE_FULLSCREEN = "fullscreen"      # 全屏置顶遮罩
MODE_WINDOW = "window"              # 普通窗口（可选置顶）
MODE_NOTIFICATION = "notification"  # 仅系统通知（托盘气泡）
MODE_PET = "pet"                    # 桌宠眨眼（无弹窗，角色眨眼 + 专属音效）
POPUP_MODES = (MODE_FULLSCREEN, MODE_WINDOW, MODE_NOTIFICATION, MODE_PET)


def _is_hex_color(text: str) -> bool:
    """校验 #RRGGBB 格式的颜色串。"""
    if len(text) != 7 or not text.startswith("#"):
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in text[1:])


def _to_int(value: Any, fallback: int) -> int:
    """宽容的 int 转换：失败时返回 fallback。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@dataclass
class AppConfig:
    """应用配置；所有字段的默认值即设置窗口的“恢复默认”。"""

    work_minutes: int = 20          # 工作时长（分钟）
    break_seconds: int = 20         # 休息时长（秒）
    popup_title: str = "该让眼睛休息啦"   # 提醒标题
    popup_message: str = "请看向 20 英尺（约 6 米）外，放松眼睛 20 秒"
    popup_mode: str = MODE_FULLSCREEN    # 提醒方式
    always_on_top: bool = True      # 普通窗口模式下是否置顶
    sound_enabled: bool = True      # 正式提醒时是否播放提示音
    eye_care_enabled: bool = True   # 启用护眼提醒（总开关）
    mask_theme: str = "teal"        # 遮罩颜色主题（mask_theme.THEMES 键或 custom）
    mask_custom_color: str = ""     # 自定义主题色（#RRGGBB，仅 custom 时生效）
    volume: int = 80                # 提示音音量（0-100，默认 80，永不为 0）
    eye_character: str = "yier"     # 护眼提醒陪伴桌宠角色
    eye_sound: str = "ding"         # 护眼提醒音（内置名或 file: 自定义路径）
    postpone_minutes: int = 5       # 推迟时长（分钟）
    dnd_enabled: bool = False       # 免打扰开关
    dnd_start: str = "22:00"        # 免打扰开始（HH:MM）
    dnd_end: str = "07:00"          # 免打扰结束（HH:MM，可跨午夜）
    autostart: bool = False         # 开机自启
    sedentary_enabled: bool = True  # 久坐提醒开关（独立于护眼提醒计时）
    sedentary_interval_minutes: int = 30  # 久坐提醒间隔（分钟）
    stand_minutes: int = 5          # 站立/活动持续时长（分钟）
    daily_stand_cap_minutes: int = 120    # 每日站立上限提醒（分钟，0=不提醒）
    stand_sound: str = "pop"        # 站立（起身）提醒音，与护眼默认不同
    sit_sound: str = "sparkle"      # 坐下提醒音，三种默认音色互不相同
    stand_message: str = "该站起来动动啦！"   # 站立提醒气泡文案
    sit_message: str = "可以坐啦~"           # 坐下提醒气泡文案
    autostart_delay_seconds: int = 30        # 开机自启延迟（秒，0=立即）
    stand_character: str = "yier"   # 站立（起身）提醒角色（characters/ 包名）
    sit_character: str = "yier"     # 坐下提醒角色（可与站立不同）
    pet_character: str = "yier"     # 常驻待机桌宠角色
    resident_pet: bool = True       # 桌宠常驻右下角待机
    pet_name: str = "夏cc"          # 桌宠名字（显示在角色下方）
    pet_home_x: int = -1            # 常驻桌宠拖拽后的位置（-1=默认右下角）
    pet_home_y: int = -1

    def sanitized(self) -> "AppConfig":
        """返回取值合法的副本：数值夹紧到安全范围，字符串补默认。"""
        cfg = AppConfig(**asdict(self))
        cfg.work_minutes = min(max(1, _to_int(cfg.work_minutes, 20)), 240)
        cfg.break_seconds = min(max(5, _to_int(cfg.break_seconds, 20)), 600)
        cfg.postpone_minutes = min(max(1, _to_int(cfg.postpone_minutes, 5)), 120)
        cfg.popup_mode = (cfg.popup_mode if cfg.popup_mode in POPUP_MODES
                          else MODE_FULLSCREEN)
        cfg.popup_title = str(cfg.popup_title).strip() or AppConfig.popup_title
        cfg.popup_message = (str(cfg.popup_message).strip()
                             or AppConfig.popup_message)
        cfg.dnd_start = str(cfg.dnd_start).strip() or "22:00"
        cfg.dnd_end = str(cfg.dnd_end).strip() or "07:00"
        cfg.sedentary_enabled = bool(cfg.sedentary_enabled)
        cfg.sedentary_interval_minutes = min(
            max(20, _to_int(cfg.sedentary_interval_minutes, 30)), 60)
        cfg.stand_minutes = min(max(3, _to_int(cfg.stand_minutes, 5)), 10)
        cfg.daily_stand_cap_minutes = min(
            max(0, _to_int(cfg.daily_stand_cap_minutes, 120)), 600)
        cfg.eye_sound = cfg.eye_sound if is_valid_sound(cfg.eye_sound) else "ding"
        cfg.stand_sound = (cfg.stand_sound if is_valid_sound(cfg.stand_sound)
                           else "pop")
        cfg.sit_sound = (cfg.sit_sound if is_valid_sound(cfg.sit_sound)
                         else "sparkle")
        cfg.pet_character = str(cfg.pet_character).strip() or "yier"
        cfg.eye_character = str(cfg.eye_character).strip() or "yier"
        cfg.eye_care_enabled = bool(cfg.eye_care_enabled)
        from mask_theme import THEMES
        cfg.mask_theme = (cfg.mask_theme if cfg.mask_theme in THEMES
                          else "teal")
        custom = str(cfg.mask_custom_color).strip()
        cfg.mask_custom_color = custom if _is_hex_color(custom) else ""
        cfg.stand_character = str(cfg.stand_character).strip() or "yier"
        cfg.stand_message = str(cfg.stand_message).strip() or "该站起来动动啦！"
        cfg.sit_message = str(cfg.sit_message).strip() or "可以坐啦~"
        cfg.autostart_delay_seconds = min(
            max(0, _to_int(cfg.autostart_delay_seconds, 30)), 300)
        cfg.sit_character = str(cfg.sit_character).strip() or "yier"
        cfg.pet_name = str(cfg.pet_name).strip() or "夏cc"
        cfg.pet_home_x = max(-1, _to_int(cfg.pet_home_x, -1))
        cfg.pet_home_y = max(-1, _to_int(cfg.pet_home_y, -1))
        cfg.resident_pet = bool(cfg.resident_pet)
        cfg.volume = min(max(0, _to_int(cfg.volume, 80)), 100)
        return cfg


def load_config() -> AppConfig:
    """读取 config.json；文件缺失或损坏时回退默认配置并记录日志。"""
    path = config_path()
    if not path.exists():
        LOG.info("配置文件不存在，使用默认配置: %s", path)
        return AppConfig()
    try:
        raw: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(AppConfig)}
        # 旧版本迁移：sedentary_sound → stand_sound；pet_voice_enabled 已移除；
        # 站立/坐下角色缺省继承原 pet_character（保持用户当前看到的形象）
        if "sedentary_sound" in raw and "stand_sound" not in raw:
            raw["stand_sound"] = raw["sedentary_sound"]
        if "pet_character" in raw:
            raw.setdefault("stand_character", raw["pet_character"])
            raw.setdefault("sit_character", raw["pet_character"])
            raw.setdefault("eye_character", raw["pet_character"])
        return AppConfig(
            **{k: v for k, v in raw.items() if k in known}).sanitized()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        LOG.warning("配置文件解析失败，使用默认配置: %s", exc)
        return AppConfig()


def save_config(cfg: AppConfig) -> bool:
    """原子化写入配置（先写 .tmp 再替换），成功返回 True。"""
    final_path = config_path()
    tmp_path = final_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(asdict(cfg.sanitized()), ensure_ascii=False, indent=2),
            encoding="utf-8")
        tmp_path.replace(final_path)
    except OSError as exc:
        LOG.error("配置写入失败: %s", exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    LOG.info("配置已保存: %s", final_path)
    return True


@dataclass
class Stats:
    """当日统计；日期变化后在 touch_today 中自动清零。"""

    date: str = ""        # YYYY-MM-DD
    breaks: int = 0       # 完成休息次数
    skipped: int = 0      # 跳过次数
    postponed: int = 0    # 推迟次数
    stand_seconds: int = 0             # 今日累计站立活动秒数
    stand_cap_notified: bool = False   # 今日是否已提示站立上限

    def touch_today(self) -> None:
        """若已跨天则重置计数并把日期对齐到今天。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.date != today:
            LOG.info("统计跨天重置: %s -> %s", self.date or "(空)", today)
            self.date = today
            self.breaks = 0
            self.skipped = 0
            self.postponed = 0
            self.stand_seconds = 0
            self.stand_cap_notified = False

    def record_break(self) -> None:
        """记录一次完成的休息并立即落盘。"""
        self.touch_today()
        self.breaks += 1
        save_stats(self)

    def record_skip(self) -> None:
        self.touch_today()
        self.skipped += 1
        save_stats(self)

    def record_postpone(self) -> None:
        self.touch_today()
        self.postponed += 1
        save_stats(self)

    def record_stand(self, seconds: int) -> None:
        """累计今日站立活动秒数并立即落盘。"""
        self.touch_today()
        self.stand_seconds += max(0, int(seconds))
        save_stats(self)

    def mark_stand_cap_notified(self) -> None:
        """记录“今日已提示站立上限”，避免重复打扰。"""
        self.stand_cap_notified = True
        save_stats(self)

    def stand_summary(self) -> str:
        """把站立秒数格式化为“X 分 Y 秒”。"""
        minutes, seconds = divmod(max(0, self.stand_seconds), 60)
        return f"{minutes} 分 {seconds} 秒"

    def summary(self) -> str:
        """统计对话框展示用的多行文本。"""
        self.touch_today()
        return (
            f"统计日期：{self.date}\n\n"
            f"完成休息：{self.breaks} 次\n"
            f"跳过休息：{self.skipped} 次\n"
            f"推迟休息：{self.postponed} 次\n"
            f"站立活动：{self.stand_summary()}"
        )


def load_stats() -> Stats:
    """读取 stats.json（缺失/损坏时重新计数），并做跨天对齐。"""
    path = stats_path()
    if not path.exists():
        return Stats()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stats = Stats(
            date=str(raw.get("date", "")),
            breaks=_to_int(raw.get("breaks", 0), 0),
            skipped=_to_int(raw.get("skipped", 0), 0),
            postponed=_to_int(raw.get("postponed", 0), 0),
            stand_seconds=_to_int(raw.get("stand_seconds", 0), 0),
            stand_cap_notified=bool(raw.get("stand_cap_notified", False)),
        )
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        LOG.warning("统计文件解析失败，重新计数: %s", exc)
        return Stats()
    stats.touch_today()
    return stats


def save_stats(stats: Stats) -> bool:
    """写入 stats.json，失败返回 False。"""
    try:
        stats_path().write_text(
            json.dumps(asdict(stats), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as exc:
        LOG.error("统计写入失败: %s", exc)
        return False
    return True
