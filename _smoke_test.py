"""EyeGuard 冒烟测试 v9：双独立状态机 + 提醒队列 + 自定义角色上传 + 全回归。

数据目录重定向到项目内 .smoke_tmp；offscreen 模式运行。
用法：python _smoke_test.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
os.environ["APPDATA"] = str(_HERE / ".smoke_tmp")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

import main  # noqa: E402
from activity import (AWAY_RESET_SECONDS, REASON_IDLE, REASON_LOCK)  # noqa: E402
from break_window import BreakWindow  # noqa: E402
from character import CharacterManager  # noqa: E402
from config import (MODE_PET, MODE_WINDOW, AppConfig, Stats,  # noqa: E402
                    load_stats)
from matting import MattingError, create_custom_pack  # noqa: E402
from pet_window import (EyeBreakPetWindow, ResidentPetWindow,  # noqa: E402
                        SedentaryPetWindow, SitDownPetWindow)
from settings_window import SettingsDialog  # noqa: E402
from timer_agent import (SED_SITTING, SED_WAITING_SIT_CONFIRM,  # noqa: E402
                         SED_WAITING_STAND_CONFIRM, SED_STANDING,
                         EYE_DISABLED, EYE_RESTING, EYE_WORKING,
                         STATE_PAUSED, STATE_WORK, TimerAgent)
from tray import TrayController  # noqa: E402
from utils import ensure_sound_files, play_sound  # noqa: E402

FAILURES: list = []


def check(name: str, ok: bool) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)
    if not ok:
        FAILURES.append(name)


def make_agent(cfg: AppConfig) -> tuple:
    agent = TimerAgent(cfg, Stats())
    monitor = agent._monitor
    monitor.idle_seconds = lambda: 0.0
    monitor._poll_screensaver = lambda: None
    monitor._poll_lock_fallback = lambda: None
    monitor._poll_idle = lambda: None
    return agent, monitor


FULL = AppConfig().work_minutes * 60

# ================= 护眼回归 =================
agent, monitor = make_agent(AppConfig())
paused_events, resumed_events = [], []
monitor.paused.connect(lambda r: paused_events.append(r))
monitor.resumed.connect(lambda a, b: resumed_events.append((a, b)))

r0 = agent.remaining_seconds()
QTest.qWait(2300)
check("活跃时倒计时递减", agent.remaining_seconds() <= r0 - 2)

monitor._apply_flag(REASON_LOCK, True)
check("锁屏后视为离开", not monitor.is_active and paused_events == [REASON_LOCK])
frozen = agent.remaining_seconds()
QTest.qWait(2300)
check("离开期间计时不增加", agent.remaining_seconds() == frozen)

monitor._apply_flag(REASON_LOCK, False)
check("解锁后重置周期且不立即触发",
      agent.remaining_seconds() == FULL and agent.eye_state == EYE_WORKING
      and resumed_events and resumed_events[-1][1] is True)

agent._remaining = 10
monitor._apply_flag(REASON_IDLE, True)
QTest.qWait(1300)
monitor._apply_flag(REASON_IDLE, False)
check("短暂空闲恢复继续且宽限到 60 秒", agent.remaining_seconds() >= 60)

agent._remaining = 300
monitor._apply_flag(REASON_IDLE, True)
monitor._absence_start = datetime.now() - timedelta(
    seconds=AWAY_RESET_SECONDS + 30)
monitor._apply_flag(REASON_IDLE, False)
check("空闲超过 1 分钟重置周期", agent.remaining_seconds() == FULL)

auto: list = []
agent.breakAutoFinished.connect(lambda: auto.append(1))
agent.trigger_break_now()
check("进入休息提醒（RESTING）", agent.eye_state == EYE_RESTING)
monitor._apply_flag(REASON_LOCK, True)
agent.on_break_done()
check("离开自动结束休息并记账",
      auto == [1] and agent.state == STATE_WORK and agent._stats.breaks == 1)
monitor._apply_flag(REASON_LOCK, False)

agent.pause()
agent._remaining = 500
monitor._apply_flag(REASON_LOCK, True)
monitor._apply_flag(REASON_LOCK, False)
check("手动暂停期间屏幕事件不重置", agent.state == STATE_PAUSED
      and agent.remaining_seconds() == 500)
agent.resume()

dnd_agent, _ = make_agent(AppConfig(
    dnd_enabled=True,
    dnd_start=(datetime.now() - timedelta(minutes=1)).strftime("%H:%M"),
    dnd_end=(datetime.now() + timedelta(minutes=1)).strftime("%H:%M")))
dnd_agent._remaining = 1
dnd_agent._eye_work_finished()
check("免打扰静默续期", dnd_agent.state == STATE_WORK)
dnd_agent.shutdown()
agent.shutdown()
check("main 模块可导入", hasattr(main, "main"))
check("TrayController 具备 hide()", hasattr(TrayController, "hide"))

# ================= 护眼总开关 =================
off_agent, _ = make_agent(AppConfig(eye_care_enabled=False))
off_agent._remaining = 1
off_agent._eye_work_finished()
check("护眼关闭后不触发提醒", off_agent.eye_state == EYE_DISABLED
      and off_agent._stats.breaks == 0)
off_agent.set_config(AppConfig(eye_care_enabled=True, work_minutes=20))
check("护眼重新开启后从 0 开始", off_agent.remaining_seconds() == 1200
      and off_agent.eye_state == EYE_WORKING)
off_agent.shutdown()

# ================= 久坐状态机 =================
sed_agent, sed_mon = make_agent(AppConfig(sedentary_enabled=True))
sed_events = {"triggered": 0, "dismissed": 0, "stand": [], "cap": []}
sed_agent.sedentaryTriggered.connect(
    lambda: sed_events.__setitem__("triggered", sed_events["triggered"] + 1))
sed_agent.sedentaryDismissed.connect(
    lambda: sed_events.__setitem__("dismissed", sed_events["dismissed"] + 1))
sed_agent.standFinished.connect(lambda p: sed_events["stand"].append(p))
sed_agent.standCapReached.connect(lambda m: sed_events["cap"].append(m))

sed_agent._sedentary_seconds = sed_agent._cfg.sedentary_interval_minutes * 60 - 2
QTest.qWait(3400)
check("久坐达到间隔触发站立提醒",
      sed_events["triggered"] == 1
      and sed_agent.sedentary_state == SED_WAITING_STAND_CONFIRM)

sed_agent._sedentary_seconds = 99999
QTest.qWait(1300)
check("等待确认期间禁止重复触发", sed_events["triggered"] == 1)

sed_agent.sedentary_snoozed(5)
check("稍后再提醒进入推迟等待",
      sed_agent.sedentary_state == SED_SITTING
      and sed_agent._sedentary_seconds
      == sed_agent._cfg.sedentary_interval_minutes * 60 - 300)

sed_agent._sedentary_seconds = sed_agent._cfg.sedentary_interval_minutes * 60
sed_agent._tick_sedentary()
check("再次到达间隔触发站立提醒", sed_agent.sedentary_state
      == SED_WAITING_STAND_CONFIRM)
sed_agent.sedentary_confirmed()
sed_agent._stand_remaining = 3
sed_agent._stand_elapsed = 0
check("确认后进入站立倒计时", sed_agent.sedentary_state == SED_STANDING)
QTest.qWait(4600)
check("站立结束触发一次坐下提醒并记账",
      sed_events["stand"] == [True]
      and sed_agent.sedentary_state == SED_WAITING_SIT_CONFIRM
      and sed_agent._stats.stand_seconds >= 3)
sed_agent.on_sit_finished()
check("坐下确认后回到新久坐周期",
      sed_agent.sedentary_state == SED_SITTING
      and sed_agent._sedentary_seconds == 0)

sed_agent._sedentary_seconds = 900
sed_mon._apply_flag(REASON_LOCK, True)
check("锁屏重置久坐周期并撤销提醒",
      sed_agent._sedentary_seconds == 0 and sed_events["dismissed"] == 1)
sed_mon._apply_flag(REASON_LOCK, False)

sed_agent._sedentary_seconds = 900
sed_mon._apply_flag(REASON_IDLE, True)
check("键鼠空闲不重置久坐", sed_agent._sedentary_seconds == 900)
sed_mon._apply_flag(REASON_IDLE, False)

sed_agent._stats.stand_cap_notified = False
sed_agent._cfg.daily_stand_cap_minutes = max(
    1, sed_agent._stats.stand_seconds // 60)
sed_agent._record_stand(60)
sed_agent._record_stand(60)
check("每日站立上限只提示一次", len(sed_events["cap"]) == 1)
sed_agent.shutdown()

sed_off, _ = make_agent(AppConfig(sedentary_enabled=False))
sed_off._sedentary_seconds = 500
QTest.qWait(1300)
check("久坐关闭后不累计", sed_off._sedentary_seconds == 500)
sed_off.set_config(AppConfig(sedentary_enabled=True))
check("久坐重新开启后从 0 开始", sed_off._sedentary_seconds == 0)
sed_off.shutdown()

# ================= 提醒队列（互斥 + 排队） =================
app2 = main.EyeGuardApp()
app2._destroy_reminder_pets()
check("队列初始为空", app2._active_reminder is None
      and not app2._reminder_queue)
check("申请护眼展示权成功", app2._acquire_reminder("eye"))
check("申请站立展示权时进入排队",
      not app2._acquire_reminder("stand") and app2._reminder_queue == ["stand"])
app2._release_reminder("eye")
check("释放后自动展示排队提醒",
      app2._active_reminder == "stand"
      and any(w.isVisible() for w in app2._sedentary_windows))
app2._destroy_reminder_pets()
app2._reminder_queue.clear()
app2._active_reminder = None
app2._agent.shutdown()

# ================= 角色包 / 自定义角色上传 =================
ensure_sound_files()
manager = CharacterManager()
packs = manager.packs()
check("内置角色包一二与布布可用", "yier" in packs and "bubu" in packs)
check("角色包为 single 单图型", all(p.kind == "single" and p.image is not None
                                 for p in packs.values()))

# 添加角色端到端（mock 系统对话框）
import settings_window as _sw  # noqa: E402
_sw.QFileDialog.getOpenFileNames = \
    lambda *a, **k: ([str(Path(os.environ["APPDATA"]) / "up.png")], "")
_sw.QInputDialog.getText = lambda *a, **k: ("我的角色", True)


class _MB:
    @staticmethod
    def information(*a, **k):
        pass

    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def critical(*a, **k):
        pass


_mb_orig = _sw.QMessageBox
_sw.QMessageBox = _MB
_up = Path(os.environ["APPDATA"]) / "up.png"
_img = QImage(200, 200, QImage.Format.Format_ARGB32)
_img.fill(QColor("#EEEEEE"))
_pt = QPainter(_img)
_pt.setBrush(QColor("#FFCC66"))
_pt.setPen(QColor("#333333"))
_pt.drawEllipse(30, 30, 140, 140)
_pt.end()
_img.save(str(_up))
dlg2 = SettingsDialog(AppConfig(), manager)
dlg2._add_custom_character(dlg2._char_combo)
check("添加自定义角色：生成包并进入桌宠形象列表",
      any(p.name == "我的角色" for p in manager.packs().values())
      and "custom" in str(dlg2._char_combo.currentData()))
dlg2.close()
dlg2.deleteLater()
_sw.QMessageBox = _mb_orig

eye_win = EyeBreakPetWindow(manager.get("yier"),
                            message="台词", countdown_seconds=20,
                            postpone_minutes=5, is_test=True,
                            auto_close_ms=3000)
eye_win.start()
QTest.qWait(1300)
check("护眼眨眼桌宠显示且气泡含台词倒计时",
      eye_win.isVisible() and eye_win._current_state() == "blink"
      and "秒后继续工作" in eye_win._bubble._message.text())
skip_events: list = []
eye_win.skipClicked.connect(lambda: skip_events.append(1))
eye_win._skip_break()
eye_win._skip_break()
check("跳过只发一次信号且窗口关闭",
      skip_events == [1] and not eye_win.isVisible())

resident = ResidentPetWindow(manager.get("yier"), name="夏cc")
resident.start()
QTest.qWait(60)
check("常驻桌宠显示且名字牌为夏cc",
      resident.isVisible() and resident._name_label.text() == "夏cc")
resident.close()

# 遮罩/窗口预览单实例
mask_events: list = []
mask1 = BreakWindow(AppConfig(popup_mode=MODE_WINDOW), is_test=True)
mask1.skipped.connect(lambda: mask_events.append("skipped"))
mask1.start()
QTest.qWait(60)
mask2 = BreakWindow(AppConfig(popup_mode=MODE_WINDOW), is_test=True)
mask2.start()
QTest.qWait(60)
mask1.dismiss_quietly()
QTest.qWait(60)
check("遮罩预览旧窗口静默关闭不发跳过",
      not mask1.isVisible() and mask2.isVisible() and mask_events == [])
mask2.dismiss_quietly()

check("统计持久化含站立数据", load_stats().stand_seconds >= 0)

# ================= 提示音 =================
ensure_sound_files()
d = AppConfig()
check("三种默认提示音互不相同", len({d.eye_sound, d.stand_sound,
                                  d.sit_sound}) == 3)
check("非法提示音回退默认", AppConfig(eye_sound="bad").sanitized()
      .eye_sound == "ding")
check("音量默认 80 且夹紧 0-100", d.volume == 80
      and AppConfig(volume=150).sanitized().volume == 100)
play_sound("none")
play_sound("ding")
check("提示音可调用", True)

print()
if FAILURES:
    print(f"共 {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("全部冒烟测试通过")
sys.exit(0)
