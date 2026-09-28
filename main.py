"""EyeGuard 入口：日志、单实例守卫、组装托盘/计时器/休息提醒/设置窗口。"""
from __future__ import annotations

import sys
from typing import List, Optional

import PySide6
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import (QApplication, QMessageBox, QSystemTrayIcon,
                               QWidget)

from break_window import BreakWindow
from character import CharacterManager, CharacterPack
from config import (MODE_NOTIFICATION, MODE_PET, AppConfig, load_config,
                    load_stats, save_stats)
from pet_window import (EyeBreakPetWindow, ResidentPetWindow,
                        SedentaryPetWindow, SitDownPetWindow)
from settings_window import SettingsDialog
from timer_agent import STATE_BREAK, TimerAgent
from tray import TrayController
from utils import (APP_VERSION, LOG, SingleInstance, build_app_icon,  # noqa: F401
                   ensure_sound_files, play_sound, play_sound_file,
                   setup_logging, sound_player, verify_audio_resources)


def _install_excepthook() -> None:
    """把未捕获异常写入日志，避免窗口模式下静默崩溃。"""
    def hook(exc_type, exc, tb) -> None:
        LOG.critical("未捕获异常: %s", exc, exc_info=(exc_type, exc, tb))
    sys.excepthook = hook


class EyeGuardApp:
    """顶层控制器：接线托盘、计时器、休息提醒与设置窗口。"""

    def __init__(self) -> None:
        self._cfg = load_config()
        self._stats = load_stats()
        self._characters = CharacterManager()
        self._agent = TimerAgent(self._cfg, self._stats)
        self._tray = TrayController(build_app_icon(), self._agent, self._stats,
                                    self._characters.names())
        self._break_windows: List[BreakWindow] = []  # 持有引用防 GC
        self._sedentary_windows: List[QWidget] = []  # 桌宠提醒/坐下提示窗口
        # ---- 统一提醒队列：同一时间只展示一个提醒，其余排队 ----
        self._active_reminder: Optional[str] = None   # "eye" / "stand" / "sit"
        self._reminder_queue: List[str] = []
        self._resident_pet: Optional[ResidentPetWindow] = None
        self._eye_pet: Optional[EyeBreakPetWindow] = None
        self._settings: Optional[SettingsDialog] = None
        sound_player().set_volume(self._cfg.volume)

        # “仅系统通知”模式下负责结束休息的计时器
        self._notif_timer = QTimer()
        self._notif_timer.setSingleShot(True)
        self._notif_timer.timeout.connect(self._notif_break_done)

        self._connect_signals()

    def _connect_signals(self) -> None:
        agent = self._agent
        agent.breakTriggered.connect(
            lambda: self._request_reminder("eye"))
        agent.breakAutoFinished.connect(self._finish_break_by_away)
        agent.sedentaryTriggered.connect(
            lambda: self._request_reminder("stand"))
        agent.sedentaryDismissed.connect(self._dismiss_sedentary)
        agent.standFinished.connect(self._on_stand_finished)
        agent.standCapReached.connect(self._on_stand_cap)

        tray = self._tray
        tray.pauseToggled.connect(agent.toggle_pause)
        tray.restNowRequested.connect(agent.trigger_break_now)
        tray.resetRequested.connect(agent.reset)
        tray.settingsRequested.connect(self.show_settings)
        tray.statsRequested.connect(self.show_stats)
        tray.quitRequested.connect(self.quit)
        tray.characterSelected.connect(self._apply_character)

        self._sync_resident_pet()

    # ---------------- 休息提醒 ----------------
    def show_break(self, test_cfg: Optional[AppConfig] = None) -> None:
        """展示休息提醒；test_cfg 非空表示来自“测试弹窗”，不接入计时。

        提醒方式互斥：桌宠眨眼 = 无弹窗，角色眨眼 break_seconds 后自动
        完成休息；全屏/普通窗口/通知方式不出现桌宠。测试预览必带音效。
        """
        cfg = test_cfg or self._cfg
        self._destroy_reminder_pets()  # 先收掉旧桌宠（同时停止旧音效）
        if test_cfg is not None or cfg.sound_enabled:
            play_sound(cfg.eye_sound)  # 新音效在销毁之后提交，不会被误停
        if test_cfg is None:
            pass  # 真实触发已在 _request_reminder 中获得展示权
        if cfg.popup_mode == MODE_PET:
            # 桌宠眨眼：无弹窗，眨眼 break_seconds 后自动完成休息
            self._show_eye_pet(cfg, test_cfg is not None,
                               cfg.break_seconds * 1000 + 600)
            if test_cfg is None:
                self._notif_timer.start(cfg.break_seconds * 1000)
            return
        if cfg.popup_mode == MODE_NOTIFICATION:
            self._tray.show_message(cfg.popup_title, cfg.popup_message)
            if test_cfg is None:
                self._notif_timer.start(cfg.break_seconds * 1000)
            return
        # 全屏遮罩 / 普通窗口（这两种方式不出现桌宠）
        window = BreakWindow(cfg, is_test=test_cfg is not None)
        self._break_windows.append(window)
        if test_cfg is None:
            window.finished.connect(self._agent.on_break_done)
            window.skipped.connect(self._agent.on_break_skipped)
            window.postponed.connect(self._agent.on_break_postponed)
        window.closed.connect(
            lambda: self._release_reminder("eye"))
        window.start()

    def _notif_break_done(self) -> None:
        """仅通知模式：气泡展示满 break_seconds 后视为完成休息。"""
        self._agent.on_break_done()

    # ---------------- 护眼桌宠（眨眼提醒） ----------------
    def _close_break_windows(self) -> None:
        """关闭全部护眼提醒窗口（遮罩/普通窗口），不发跳过/推迟信号。"""
        for window in self._break_windows:
            window.dismiss_quietly()
        self._break_windows.clear()

    def _destroy_reminder_pets(self) -> None:
        """销毁全部提醒界面（遮罩/窗口、眨眼桌宠、站立/坐下桌宠），
        保证任意时刻只有一个活跃提醒实例。"""
        self._close_break_windows()
        self._close_eye_pet()
        for window in self._sedentary_windows:
            window.dismiss_quietly()
        self._sedentary_windows.clear()

    def _show_eye_pet(self, cfg: AppConfig, is_test: bool,
                      auto_close_ms: Optional[int] = None) -> None:
        """桌宠眨眼提醒方式：无弹窗，角色眨眼 + 台词气泡 + 休息倒计时。"""
        pack = self._characters.get(cfg.eye_character)
        if pack is None:
            LOG.warning("角色包缺失（%s），护眼提醒未展示", cfg.eye_character)
            return
        self._hide_resident_for_reminder()
        self._destroy_reminder_pets()
        self._eye_pet = EyeBreakPetWindow(
            pack, message=cfg.popup_message,
            countdown_seconds=cfg.break_seconds,
            postpone_minutes=cfg.postpone_minutes,
            is_test=is_test, auto_close_ms=auto_close_ms)
        self._eye_pet.set_name(cfg.pet_name)
        self._eye_pet.closed.connect(self._reshow_resident_later)
        self._eye_pet.closed.connect(sound_player().stop)
        self._eye_pet.closed.connect(
            lambda: self._release_reminder("eye"))
        if not is_test:  # 真实提醒：跳过/推迟接入计时与统计
            self._eye_pet.skipClicked.connect(self._on_eye_pet_skip)
            self._eye_pet.postponeClicked.connect(self._on_eye_pet_postpone)
        self._eye_pet.start()
        LOG.info("护眼桌宠已显示（%s，眨眼 %d 秒）",
                 pack.name, cfg.break_seconds)

    def _on_eye_pet_skip(self) -> None:
        """桌宠眨眼提醒被跳过：记账并立即开始下一轮工作。"""
        self._notif_timer.stop()
        self._agent.on_break_skipped()

    def _on_eye_pet_postpone(self) -> None:
        """桌宠眨眼提醒被推迟：N 分钟后重新提醒。"""
        self._notif_timer.stop()
        self._agent.on_break_postponed()

    def _close_eye_pet(self) -> None:
        """收回护眼桌宠。"""
        if self._eye_pet is not None:
            self._eye_pet.dismiss_quietly()
            self._eye_pet = None

    def _finish_break_by_away(self) -> None:
        """休息期间锁屏/屏保/关屏/睡眠：关掉休息窗口，真实休息按完成记账。"""
        real_break = self._notif_timer.isActive()
        self._notif_timer.stop()
        for window in self._break_windows:
            if not window.isVisible():
                continue
            if not window.is_test:
                real_break = True
            # 先断开结果信号再关闭，避免 closeEvent 误发 skipped 影响统计
            for sig in (window.finished, window.skipped, window.postponed):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
            window.close()
        self._break_windows.clear()
        self._destroy_reminder_pets()
        self._reminder_queue.clear()
        if real_break:
            self._agent.on_break_done()

    # ---------------- 久坐提醒 ----------------
    def _play_reminder_sound(self, kind: str, cfg: AppConfig) -> None:
        """播放站立/坐下提醒音：对应角色包自带音效优先，否则用配置音效键。"""
        pack = self._characters.get(cfg.stand_character if kind == "stand"
                                    else cfg.sit_character)
        pack_sound = pack.sounds.get(kind) if pack else None
        if pack_sound is not None:
            play_sound_file(pack_sound)
        else:
            play_sound(cfg.stand_sound if kind == "stand"
                       else cfg.sit_sound)

    def show_sedentary_reminder(self,
                                test_cfg: Optional[AppConfig] = None) -> None:
        """展示久坐起身提醒（桌宠动画）；护眼休息进行中则排队，避免互相覆盖。

        测试预览（test_cfg 非空）无论“提醒时播放提示音”开关如何都必带音效。
        """
        cfg = test_cfg or self._cfg
        pack = self._characters.get(cfg.stand_character)
        if pack is None:
            LOG.warning("角色包缺失（%s），站立提醒未展示", cfg.stand_character)
            return
        self._hide_resident_for_reminder()
        self._destroy_reminder_pets()  # 先销毁旧桌宠（停止旧音效）
        if test_cfg is not None or cfg.sound_enabled:
            self._play_reminder_sound("stand", cfg)  # 新音效最后提交
        window = SedentaryPetWindow(pack, is_test=test_cfg is not None)
        window.set_name(cfg.pet_name)
        self._sedentary_windows.append(window)
        window.closed.connect(self._reshow_resident_later)
        window.closed.connect(sound_player().stop)
        window.closed.connect(lambda: self._release_reminder("stand"))
        if test_cfg is None:
            window.confirmed.connect(self._agent.sedentary_confirmed)
            window.snoozed.connect(self._agent.sedentary_snoozed)
        window.start()

    def show_sit_reminder(self, test_cfg: Optional[AppConfig] = None) -> None:
        """坐下提醒预览/展示：坐下角色 + 坐下音效（供“测试坐下提醒”）。"""
        cfg = test_cfg or self._cfg
        pack = self._characters.get(cfg.sit_character)
        if pack is None:
            LOG.warning("角色包缺失（%s），坐下提醒未展示", cfg.sit_character)
            return
        self._hide_resident_for_reminder()
        self._destroy_reminder_pets()  # 先销毁旧桌宠（停止旧音效）
        self._active_reminder = "sit"  # 坐下提醒属于站立流程的收尾
        if test_cfg is not None or cfg.sound_enabled:
            self._play_reminder_sound("sit", cfg)  # 新音效最后提交
        window = SitDownPetWindow(pack, cfg.stand_minutes * 60)
        window.set_name(cfg.pet_name)
        self._sedentary_windows.append(window)
        window.closed.connect(self._reshow_resident_later)
        window.closed.connect(sound_player().stop)
        window.closed.connect(lambda: self._release_reminder("sit"))
        window.start()

    # ---------------- 统一提醒队列 ----------------
    def _request_reminder(self, kind: str) -> None:
        """真实提醒触发入口：经队列保证同时只展示一个提醒。"""
        if not self._acquire_reminder(kind):
            return
        if kind == "eye":
            self.show_break(None)
        elif kind == "stand":
            self.show_sedentary_reminder(None)

    def _acquire_reminder(self, kind: str) -> bool:
        """申请展示权；已有提醒在展示时把新提醒排入队列。"""
        if self._active_reminder is None or self._active_reminder == kind:
            self._active_reminder = kind
            return True
        self._reminder_queue.append(kind)
        LOG.info("提醒 %s 进入队列（正在展示：%s）", kind, self._active_reminder)
        return False

    def _release_reminder(self, kind: str) -> None:
        """释放展示权并立即展示队列中的下一个提醒。"""
        if self._active_reminder == kind:
            self._active_reminder = None
            self._dispatch_next_reminder()

    def _dispatch_next_reminder(self) -> None:
        if self._active_reminder is not None or not self._reminder_queue:
            return
        kind = self._reminder_queue.pop(0)
        LOG.info("提醒队列：开始展示 %s", kind)
        self._active_reminder = kind
        if kind == "eye":
            self.show_break(None)
        elif kind == "stand":
            self.show_sedentary_reminder(None)

    def _dismiss_sedentary(self) -> None:
        """用户离开屏幕：撤销久坐提醒/坐下提示与排队（周期已由代理重置）。"""
        self._destroy_reminder_pets()
        self._reminder_queue.clear()
        self._reshow_resident_later(1500)

    def _on_stand_finished(self, screen_present: bool) -> None:
        """站立倒计时结束：仅当用户在屏幕前才提示“可以坐下了”。"""
        if not screen_present:
            LOG.info("站立结束时用户不在屏幕前，跳过坐下提示")
            return
        pack = self._characters.get(self._cfg.sit_character)
        if pack is None:
            return
        self._hide_resident_for_reminder()
        self._destroy_reminder_pets()  # 先销毁旧桌宠（停止旧音效）
        self._active_reminder = "sit"  # 坐下提醒属于站立流程的收尾
        if self._cfg.sound_enabled:
            self._play_reminder_sound("sit", self._cfg)  # 新音效最后提交
        window = SitDownPetWindow(pack, self._agent.last_stand_seconds)
        window.set_name(self._cfg.pet_name)
        self._sedentary_windows.append(window)
        window.closed.connect(self._reshow_resident_later)
        window.closed.connect(sound_player().stop)
        window.closed.connect(lambda: self._release_reminder("sit"))
        window.start()

    def _on_pet_home_moved(self, x: int, y: int) -> None:
        """常驻桌宠被拖到新位置：持久化到配置。"""
        self._cfg.pet_home_x = x
        self._cfg.pet_home_y = y
        save_config(self._cfg)
        LOG.info("常驻桌宠位置已保存: (%d, %d)", x, y)

    def _on_stand_cap(self, minutes_today: int) -> None:
        """今日站立累计达到设定上限：托盘温和提示一次。"""
        self._tray.show_message(
            "久坐提醒 · 每日站立上限",
            f"今日累计站立活动已达 {minutes_today} 分钟，达到设定上限。\n"
            "建议每日站立/轻度活动总量控制在 2–4 小时内，注意劳逸结合。")

    # ---------------- 设置 ----------------
    def show_settings(self) -> None:
        """打开设置窗口（单例：重复打开只前置已有窗口）。"""
        if self._settings is not None:
            self._settings.raise_()
            self._settings.activateWindow()
            return
        dialog = SettingsDialog(self._cfg, self._characters)
        dialog.saved.connect(self._on_config_saved)
        dialog.testPopup.connect(self.show_break)
        dialog.testSedentary.connect(self.show_sedentary_reminder)
        dialog.testSit.connect(self.show_sit_reminder)
        dialog.characterChanged.connect(self._apply_character)
        dialog.finished.connect(lambda _result: self._on_settings_closed())
        self._settings = dialog
        dialog.show()

    def _on_settings_closed(self) -> None:
        dialog, self._settings = self._settings, None
        if dialog is not None:
            dialog.deleteLater()

    def _on_config_saved(self, cfg: AppConfig) -> None:
        """设置保存后：热应用到计时器、桌宠与托盘。"""
        self._cfg = cfg
        self._agent.set_config(cfg)
        sound_player().set_volume(cfg.volume)
        # 关闭护眼提醒：收回遮罩/眨眼桌宠，清掉护眼排队项并停止音效
        if not cfg.eye_care_enabled:
            self._close_break_windows()
            self._close_eye_pet()
            self._reminder_queue = [k for k in self._reminder_queue
                                    if k != "eye"]
            if self._active_reminder == "eye":
                self._active_reminder = None
                self._dispatch_next_reminder()
            sound_player().stop()
        # 关闭久坐提醒：收回站立/坐下桌宠，清掉久坐排队项并停止音效
        if not cfg.sedentary_enabled:
            for w in list(self._sedentary_windows):
                w.dismiss_quietly()
            self._sedentary_windows.clear()
            self._reminder_queue = [k for k in self._reminder_queue
                                    if k != "stand"]
            if self._active_reminder == "stand":
                self._active_reminder = None
                self._dispatch_next_reminder()
            sound_player().stop()
        if self._resident_pet is not None:
            self._resident_pet.set_name(cfg.pet_name)
        self._tray.refresh()
        self._tray.sync_character(cfg.pet_character)
        self._sync_resident_pet()
        LOG.info("设置已生效")

    # ---------------- 角色包 / 常驻桌宠 ----------------
    def _apply_character(self, folder: str) -> None:
        """切换桌宠角色：立即生效并自动保存（不影响计时逻辑）。"""
        pack = self._characters.get(folder)
        if pack is None:
            LOG.warning("角色包不存在：%s", folder)
            return
        changed = self._cfg.pet_character != pack.folder
        self._cfg.pet_character = pack.folder
        if changed:
            save_config(self._cfg)
        self._tray.sync_character(pack.folder)
        if self._resident_pet is not None:
            self._resident_pet.set_pack(pack)
        LOG.info("桌宠形象切换为：%s（%s）", pack.name, pack.folder)

    def _sync_resident_pet(self) -> None:
        """按配置创建/销毁常驻待机桌宠。"""
        if self._cfg.resident_pet and self._resident_pet is None:
            pack = self._characters.get(self._cfg.pet_character)
            if pack is None:
                return
            home = None
            if self._cfg.pet_home_x >= 0 and self._cfg.pet_home_y >= 0:
                home = QPoint(self._cfg.pet_home_x, self._cfg.pet_home_y)
            self._resident_pet = ResidentPetWindow(
                pack, name=self._cfg.pet_name, home=home)
            self._resident_pet.homeMoved.connect(self._on_pet_home_moved)
            self._resident_pet.start()
            LOG.info("常驻桌宠已显示（%s）", pack.name)
        elif not self._cfg.resident_pet and self._resident_pet is not None:
            self._resident_pet.close()
            self._resident_pet.deleteLater()
            self._resident_pet = None
            LOG.info("常驻桌宠已关闭")

    def _hide_resident_for_reminder(self) -> None:
        """提醒动画期间临时隐藏常驻桌宠，避免同屏出现两只。"""
        if self._resident_pet is not None and self._resident_pet.isVisible():
            self._resident_pet.hide()

    def _reshow_resident_later(self, delay_ms: int = 2000) -> None:
        """提醒流程结束后恢复常驻桌宠（留出退场动画时间）。"""
        if self._resident_pet is None or not self._cfg.resident_pet:
            return
        QTimer.singleShot(delay_ms, self._show_resident)

    def _show_resident(self) -> None:
        if (self._resident_pet is not None and self._cfg.resident_pet
                and not any(w.isVisible() for w in self._sedentary_windows)):
            self._resident_pet.show()

    # ---------------- 统计 / 退出 ----------------
    def show_stats(self) -> None:
        QMessageBox.information(
            None, "EyeGuard · 今日统计", self._stats.summary())

    def quit(self) -> None:
        LOG.info("程序退出")
        save_stats(self._stats)
        self._agent.shutdown()
        if self._resident_pet is not None:
            self._resident_pet.close()
        self._close_eye_pet()
        self._tray.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()


def main() -> int:
    debug = "--debug" in sys.argv
    setup_logging(debug=debug)
    _install_excepthook()
    ensure_sound_files()
    verify_audio_resources()
    LOG.info("EyeGuard %s 启动（Python %s，PySide6 %s）", APP_VERSION,
             sys.version.split()[0], PySide6.__version__)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关闭弹窗/设置不能退出托盘程序
    app.setApplicationName("EyeGuard")
    app.setApplicationDisplayName("EyeGuard")
    app.setWindowIcon(build_app_icon())

    if not QSystemTrayIcon.isSystemTrayAvailable():
        LOG.error("系统托盘不可用，程序退出")
        QMessageBox.critical(
            None, "EyeGuard", "当前系统不支持托盘图标，程序退出。")
        return 1

    guard = SingleInstance("EyeGuard.SingleInstance")
    if not guard.try_lock():
        LOG.warning("检测到已有实例，本次启动直接退出")
        box = QMessageBox(
            QMessageBox.Icon.Information, "EyeGuard",
            "EyeGuard 已在运行，请查看系统托盘。\n（本提示 8 秒后自动关闭）")
        QTimer.singleShot(8000, box.close)
        box.exec()
        return 0
    app.aboutToQuit.connect(guard.release)

    controller = EyeGuardApp()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
