"""计时代理 v3：护眼与久坐两条完全独立的状态机 + 显式状态名。

护眼状态机：EYE_DISABLED → EYE_IDLE → EYE_WORKING → EYE_RESTING(提醒+倒计时)
    → EYE_WORKING（下一周期）。仅“启用护眼提醒”且屏幕活跃时累计；
    离开屏幕（锁屏/屏保/关屏/睡眠/会话断开）恢复后重置当前周期。

久坐状态机：SED_DISABLED → SED_SITTING → SED_WAITING_STAND_CONFIRM（等待已起身）
    → SED_STANDING（站立倒计时）→ SED_WAITING_SIT_CONFIRM（等待确认坐下/超时）
    → SED_SITTING（下一周期）。SED_REMINDING_STAND / SED_REMINDING_SIT 为瞬时状态，
    仅用于触发展示与日志。任何离开状态都会重置久坐周期。

两条状态机互不读写对方状态；提醒展示的互斥由主程序的提醒队列保证。
手动“暂停计时”同时冻结两条状态机的累计。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from activity import (BIG_REASONS, RESUME_GRACE_SECONDS,
                      REASON_TEXT, ScreenActivityMonitor)
from config import AppConfig, Stats
from utils import LOG, in_dnd

STATE_WORK = "work"      # 护眼：工作计时中（兼容托盘显示）
STATE_PAUSED = "paused"  # 护眼：已手动暂停（同时冻结久坐累计）
STATE_BREAK = "break"    # 护眼：休息提醒展示中（兼容托盘显示）

# 护眼状态机
EYE_DISABLED = "eye_disabled"
EYE_IDLE = "eye_idle"
EYE_WORKING = "eye_working"
EYE_RESTING = "eye_resting"          # 提醒展示中，休息倒计时进行
# 久坐状态机
SED_DISABLED = "sed_disabled"
SED_SITTING = "sed_sitting"
SED_WAITING_STAND_CONFIRM = "sed_waiting_stand_confirm"  # 等待“已起身”
SED_STANDING = "sed_standing"                            # 站立倒计时
SED_WAITING_SIT_CONFIRM = "sed_waiting_sit_confirm"      # 等待确认坐下/超时

SED_REMINDING_STAND = "sed_reminding_stand"  # 瞬时状态：站立提醒已触发
SED_REMINDING_SIT = "sed_reminding_sit"      # 瞬时状态：坐下提醒已触发

SNOOZE_MINUTES = 5  # 起身提醒“稍后再提醒”的推迟时长


class TimerAgent(QObject):
    """秒级 tick 驱动的双状态机（护眼 / 久坐），两条链路完全独立。

    对外信号：
        workTick(int)          护眼工作剩余秒数（托盘提示用）
        stateChanged(str)      护眼粗状态（work/paused/break，托盘用）
        eyeStateChanged(str)   护眼细状态（EYE_*）
        sedStateChanged(str)   久坐细状态（SED_*）
        breakTriggered()       需要展示护眼提醒
        breakAutoFinished()    护眼休息期间检测到离开，主程序应收尾休息
        statsChanged()         护眼统计变化
        sedentaryTriggered()   久坐时间到，需要展示站立提醒
        sedentaryDismissed()   用户离开屏幕：撤销久坐提醒与排队
        standFinished(bool)    站立倒计时结束（参数：用户是否在屏幕前）
        standCapReached(int)   今日站立累计达到上限（参数：今日分钟数）
    """

    workTick = Signal(int)
    stateChanged = Signal(str)
    eyeStateChanged = Signal(str)
    sedStateChanged = Signal(str)
    breakTriggered = Signal()
    breakAutoFinished = Signal()
    statsChanged = Signal()
    sedentaryTriggered = Signal()
    sedentaryDismissed = Signal()
    standFinished = Signal(bool)
    standCapReached = Signal(int)

    def __init__(self, cfg: AppConfig, stats: Stats,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._stats = stats
        self._state = STATE_WORK              # 护眼粗状态（托盘/手动暂停）
        self._eye_state = EYE_WORKING         # 护眼细状态
        self._remaining = cfg.work_minutes * 60
        self._sed_state = SED_SITTING if cfg.sedentary_enabled else SED_DISABLED
        self._sedentary_seconds = 0
        self._stand_remaining = 0
        self._stand_elapsed = 0
        self._last_stand_seconds = 0
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._monitor = ScreenActivityMonitor(self)
        self._monitor.paused.connect(self.on_screen_paused)
        self._monitor.resumed.connect(self.on_screen_resumed)
        self.start_work_cycle()
        if not cfg.eye_care_enabled:
            self._eye_state = EYE_DISABLED
        if not cfg.sedentary_enabled:
            self._sed_state = SED_DISABLED
            self._sedentary_seconds = 0
        LOG.info("护眼提醒%s：每 %d 分钟，休息 %d 秒；久坐提醒%s：每 %d 分钟，"
                 "站立 %d 分钟，每日上限 %d 分钟",
                 "已启用" if cfg.eye_care_enabled else "已关闭",
                 cfg.work_minutes, cfg.break_seconds,
                 "已启用" if cfg.sedentary_enabled else "已关闭",
                 cfg.sedentary_interval_minutes, cfg.stand_minutes,
                 cfg.daily_stand_cap_minutes)

    # ---------------- 查询 ----------------
    @property
    def state(self) -> str:
        """护眼粗状态：work / paused / break。"""
        return self._state

    def remaining_seconds(self) -> int:
        """护眼当前工作周期剩余的活跃秒数。"""
        return max(0, int(self._remaining))

    @property
    def eye_state(self) -> str:
        """护眼细状态（EYE_*）。"""
        return self._eye_state

    @property
    def sedentary_state(self) -> str:
        """久坐细状态。"""
        return self._sed_state

    @property
    def last_stand_seconds(self) -> int:
        """最近一次站立的实际时长（秒）。"""
        return self._last_stand_seconds

    # ---------------- 护眼：周期控制 ----------------
    def start_work_cycle(self, minutes: Optional[float] = None) -> None:
        """开始/重置一轮护眼工作计时；minutes 缺省用配置的工作时长。"""
        mins = self._cfg.work_minutes if minutes is None else minutes
        self._remaining = int(round(mins * 60))
        self._set_state(STATE_WORK)
        self._set_eye_state(EYE_WORKING)
        self._tick.start()
        self.workTick.emit(self.remaining_seconds())
        LOG.info("护眼：工作计时开始，活跃使用 %.1f 分钟后提醒休息", mins)

    def pause(self) -> None:
        """手动暂停：冻结护眼与久坐两条状态机的累计。"""
        if self._state in (STATE_PAUSED, STATE_BREAK):
            return
        self._set_state(STATE_PAUSED)
        LOG.info("计时已手动暂停（护眼与久坐累计同时暂停），剩余 %d 秒",
                 self.remaining_seconds())

    def resume(self) -> None:
        """手动恢复计时（仅暂停状态有效）。"""
        if self._state != STATE_PAUSED:
            return
        self._set_state(STATE_WORK)
        self.workTick.emit(self.remaining_seconds())
        LOG.info("计时已手动恢复")

    def toggle_pause(self) -> None:
        if self._state == STATE_PAUSED:
            self.resume()
        else:
            self.pause()

    def reset(self) -> None:
        """重置为完整的最新护眼工作周期。"""
        LOG.info("计时已重置")
        self.start_work_cycle()

    def set_config(self, cfg: AppConfig) -> None:
        """应用新配置（含护眼/久坐两个总开关的启用与停用）。"""
        eye_was = self._eye_state != EYE_DISABLED
        sed_was = self._sed_state != SED_DISABLED
        self._cfg = cfg
        if self._state == STATE_PAUSED:
            self._remaining = min(self._remaining, cfg.work_minutes * 60)
        elif self._state != STATE_BREAK:
            self.start_work_cycle()
        # ---- 护眼开关 ----
        if not cfg.eye_care_enabled:
            self._eye_state = EYE_DISABLED
            self._remaining = cfg.work_minutes * 60  # 重新开启时从 0 开始
            self._set_state(STATE_WORK)  # 若在提醒/休息中被关闭，退出提醒态
            LOG.info("护眼提醒已关闭：计时清零，不再触发护眼提醒")
        elif not eye_was:
            self._eye_state = EYE_WORKING
            self._remaining = cfg.work_minutes * 60
            LOG.info("护眼提醒已开启：从 0 开始计时")
        # ---- 久坐开关 ----
        if not cfg.sedentary_enabled:
            self._sed_state = SED_DISABLED
            self._sedentary_seconds = 0
            self.sedentaryDismissed.emit()
            LOG.info("久坐提醒已关闭：周期清零，不再触发站立/坐下提醒")
        elif not sed_was:
            self._sed_state = SED_SITTING
            self._sedentary_seconds = 0
            LOG.info("久坐提醒已开启：从 0 开始计时")
        elif self._sed_state == SED_SITTING:
            self._sedentary_seconds = 0  # 参数变化，周期重新计时
        LOG.info("新配置已应用（护眼 %d 分钟/休息 %d 秒；久坐每 %d 分钟）",
                 cfg.work_minutes, cfg.break_seconds,
                 cfg.sedentary_interval_minutes)

    def shutdown(self) -> None:
        """停止计时与屏幕监控（程序退出前调用）。"""
        self._tick.stop()
        self._monitor.shutdown()

    # ---------------- 护眼：tick 与触发 ----------------
    def _on_tick(self) -> None:
        self._tick_eye()
        self._tick_sedentary()

    def _tick_eye(self) -> None:
        """护眼状态机推进。"""
        if not self._cfg.eye_care_enabled or self._eye_state == EYE_DISABLED:
            return
        if self._eye_state == EYE_WORKING:
            if self._state != STATE_WORK or not self._monitor.is_active:
                return  # 手动暂停或屏幕离开：本秒不累计
            self._remaining -= 1
            self.workTick.emit(self.remaining_seconds())
            if self._remaining <= 0:
                self._eye_work_finished()
        # EYE_RESTING：提醒已展示，倒计时由提醒窗口自行驱动，
        # 结束时通过 on_break_done/skipped/postponed 回到 EYE_WORKING。

    def _eye_work_finished(self) -> None:
        """活跃时间用尽：免打扰静默续期，否则进入提醒。"""
        cfg = self._cfg
        if not cfg.eye_care_enabled:
            return
        if cfg.dnd_enabled and in_dnd(cfg.dnd_start, cfg.dnd_end):
            LOG.info("免打扰时段 %s-%s，本轮护眼提醒静默跳过",
                     cfg.dnd_start, cfg.dnd_end)
            self.start_work_cycle()
            return
        self.trigger_break_now()

    def trigger_break_now(self) -> None:
        """立即触发护眼提醒（托盘“立即休息”或活跃时间用尽）。"""
        if not self._cfg.eye_care_enabled:
            LOG.info("护眼提醒已关闭，忽略休息请求")
            return
        if self._eye_state in (EYE_RESTING,):
            LOG.info("护眼提醒正在展示，忽略重复触发")
            return
        self._tick_eye_to_reminding()

    def _tick_eye_to_reminding(self) -> None:
        self._set_state(STATE_BREAK)
        self._set_eye_state(EYE_RESTING)
        LOG.info("触发护眼提醒（休息倒计时 %d 秒）", self._cfg.break_seconds)
        self.breakTriggered.emit()

    # ---------------- 护眼：屏幕活跃度回调 ----------------
    def on_screen_paused(self, reason: str) -> None:
        """屏幕进入离开状态：护眼冻结，久坐重置周期。"""
        text = REASON_TEXT.get(reason, reason)
        if self._state == STATE_BREAK or self._eye_state == EYE_RESTING:
            LOG.info("休息期间检测到离开（%s），视为已完成休息", text)
            self.breakAutoFinished.emit()
        elif self._state == STATE_WORK:
            LOG.info("屏幕未在使用（%s），护眼计时暂停", text)
        if reason in BIG_REASONS:
            self._reset_sedentary_by_away(text)

    def on_screen_resumed(self, away_seconds: float, big_state: bool) -> None:
        """回到屏幕：护眼按离开时长重置或继续（均不立即触发提醒）。"""
        if self._state != STATE_WORK:
            return
        if big_state or away_seconds > self._cfg.away_reset_seconds:
            LOG.info("离开 %.0f 秒%s，视为眼睛已休息，重置护眼周期",
                     away_seconds,
                     "（经过锁屏/屏保/关屏/睡眠/会话断开）" if big_state else "")
            self.start_work_cycle()
        else:
            # 短暂空闲：继续剩余倒计时，但保底宽限，避免一回来就弹窗
            self._remaining = max(self._remaining, RESUME_GRACE_SECONDS)
            self.workTick.emit(self.remaining_seconds())
            LOG.info("短暂离开 %.0f 秒后恢复，继续计时（剩余 %d 秒）",
                     away_seconds, self._remaining)

    # ---------------- 护眼：提醒结果回叫 ----------------
    def on_break_done(self) -> None:
        """休息倒计时自然结束（或离开期间视为完成）。"""
        self._stats.record_break()
        self.statsChanged.emit()
        LOG.info("完成一次护眼休息")
        self.start_work_cycle()

    def on_break_skipped(self) -> None:
        """用户跳过本次护眼休息。"""
        self._stats.record_skip()
        self.statsChanged.emit()
        LOG.info("跳过一次护眼休息")
        self.start_work_cycle()

    def on_break_postponed(self) -> None:
        """用户推迟护眼提醒：按推迟时长重新计一轮工作。"""
        self._stats.record_postpone()
        self.statsChanged.emit()
        LOG.info("推迟护眼提醒 %d 分钟", self._cfg.postpone_minutes)
        self.start_work_cycle(minutes=self._cfg.postpone_minutes)

    # ---------------- 久坐：状态机 ----------------
    def _set_sed_state(self, state: str) -> None:
        if self._sed_state != state:
            self._sed_state = state
            self.sedStateChanged.emit(state)

    def sedentary_confirmed(self) -> None:
        """用户点击“已起身”：进入站立倒计时（真实时间流逝）。"""
        if self._sed_state != SED_WAITING_STAND_CONFIRM:
            return
        self._set_sed_state(SED_STANDING)
        self._stand_remaining = self._cfg.stand_minutes * 60
        self._stand_elapsed = 0
        LOG.info("用户确认已起身，开始 %d 分钟站立倒计时",
                 self._cfg.stand_minutes)

    def sedentary_snoozed(self, minutes: int = SNOOZE_MINUTES) -> None:
        """“稍后再提醒”：推迟 N 分钟后只再提醒一次。"""
        if self._sed_state != SED_WAITING_STAND_CONFIRM:
            return
        interval = self._cfg.sedentary_interval_minutes * 60
        self._sedentary_seconds = max(0, interval - minutes * 60)
        self._set_sed_state(SED_SITTING)
        LOG.info("站立提醒推迟 %d 分钟（到点只再提醒一次）", minutes)

    def on_sit_finished(self) -> None:
        """坐下提醒被确认或超时：回到 SITTING，开始下一个久坐周期。"""
        if self._sed_state != SED_WAITING_SIT_CONFIRM:
            return
        self._set_sed_state(SED_SITTING)
        self._sedentary_seconds = 0
        LOG.info("坐下提醒结束，久坐周期重新开始")

    def _tick_sedentary(self) -> None:
        """久坐状态机每秒推进。"""
        cfg = self._cfg
        if not cfg.sedentary_enabled or self._sed_state == SED_DISABLED:
            return
        if self._sed_state == SED_SITTING:
            # 手动暂停不累计；键鼠空闲照常累计（看视频也属于久坐）
            if (self._state != STATE_PAUSED
                    and self._monitor.screen_present):
                self._sedentary_seconds += 1
                if self._sedentary_seconds >= \
                        cfg.sedentary_interval_minutes * 60:
                    self._set_sed_state(SED_REMINDING_STAND)
                    LOG.info("触发站立提醒（已连续坐着 %d 分钟）",
                             cfg.sedentary_interval_minutes)
                    self._set_sed_state(SED_WAITING_STAND_CONFIRM)
                    self.sedentaryTriggered.emit()
        elif self._sed_state == SED_STANDING:
            # 站立倒计时按真实时间流逝，与屏幕状态无关
            self._stand_remaining -= 1
            self._stand_elapsed += 1
            if self._stand_remaining <= 0:
                self._finish_stand()

    def _finish_stand(self) -> None:
        """站立倒计时结束：触发一次坐下提醒。"""
        self._last_stand_seconds = self._stand_elapsed
        self._record_stand(self._stand_elapsed)
        self._set_sed_state(SED_REMINDING_SIT)
        LOG.info("站立结束（%d 秒），触发坐下提醒", self._last_stand_seconds)
        self._set_sed_state(SED_WAITING_SIT_CONFIRM)
        self.standFinished.emit(self._monitor.screen_present)

    def _record_stand(self, seconds: int) -> None:
        """累计今日站立时长；越过每日上限时提示一次。"""
        if seconds <= 0:
            return
        self._stats.record_stand(seconds)
        cap = self._cfg.daily_stand_cap_minutes
        if (cap > 0 and not self._stats.stand_cap_notified
                and self._stats.stand_seconds >= cap * 60):
            self._stats.mark_stand_cap_notified()
            LOG.info("今日站立活动达到上限 %d 分钟", cap)
            self.standCapReached.emit(self._stats.stand_seconds // 60)

    def _reset_sedentary_by_away(self, text: str) -> None:
        """离开屏幕：任何久坐状态都重置周期（离开=已经起身活动）。"""
        if self._sed_state == SED_STANDING:
            LOG.info("离开屏幕结束站立（已站 %d 秒），计入今日站立活动",
                     self._stand_elapsed)
            self._record_stand(self._stand_elapsed)
        self._set_sed_state(SED_SITTING)
        self._sedentary_seconds = 0
        self.sedentaryDismissed.emit()

    def on_screen_paused_sedentary_guard(self) -> None:
        """占位：久坐重置统一走 on_screen_paused → _reset_sedentary_by_away。"""

    # ---------------- 久坐：由提醒窗口回叫 ----------------
    def on_stand_timeout(self) -> None:
        """站立提醒超时未确认（自动按“稍后再提醒”处理）。由主程序调用。"""
        self.sedentary_snoozed()

    def on_sit_confirmed(self) -> None:
        """用户确认坐下（点击桌宠）。"""
        self.on_sit_finished()

    def shutdown(self) -> None:
        self._tick.stop()
        self._monitor.shutdown()

    # ---------------- 内部 ----------------
    def _set_state(self, state: str) -> None:
        if self._state != state:
            self._state = state
            self.stateChanged.emit(state)

    def _set_eye_state(self, state: str) -> None:
        if self._eye_state != state:
            self._eye_state = state
            self.eyeStateChanged.emit(state)
