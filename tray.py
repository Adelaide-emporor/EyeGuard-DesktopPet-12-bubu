"""系统托盘：图标、右键菜单（含切换角色）、气泡通知与倒计时提示。"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from config import Stats
from timer_agent import STATE_BREAK, STATE_PAUSED, TimerAgent


class TrayController(QObject):
    """封装 QSystemTrayIcon，把菜单动作翻译成信号供主控制器接线。"""

    pauseToggled = Signal()
    restNowRequested = Signal()
    resetRequested = Signal()
    settingsRequested = Signal()
    statsRequested = Signal()
    quitRequested = Signal()
    characterSelected = Signal(str)

    def __init__(self, icon: QIcon, agent: TimerAgent, stats: Stats,
                 characters: Optional[List[Tuple[str, str]]] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._agent = agent
        self._stats = stats

        self._act_pause = QAction("暂停计时", self)
        act_rest = QAction("立即休息", self)
        act_reset = QAction("重置计时", self)
        act_settings = QAction("设置...", self)
        act_stats = QAction("今日统计...", self)
        act_quit = QAction("退出", self)
        self._act_pause.triggered.connect(self.pauseToggled)
        act_rest.triggered.connect(self.restNowRequested)
        act_reset.triggered.connect(self.resetRequested)
        act_settings.triggered.connect(self.settingsRequested)
        act_stats.triggered.connect(self.statsRequested)
        act_quit.triggered.connect(self.quitRequested)

        menu = QMenu()
        menu.addAction(self._act_pause)
        menu.addAction(act_rest)
        menu.addAction(act_reset)
        menu.addSeparator()

        # 切换角色子菜单（位于功能项与设置之间）
        self._character_actions: List[Tuple[str, QAction]] = []
        if characters:
            char_menu = menu.addMenu("切换角色")
            for folder, name in characters:
                action = QAction(name, char_menu)
                action.setCheckable(True)
                action.triggered.connect(
                    lambda _c=False, f=folder: self.characterSelected.emit(f))
                char_menu.addAction(action)
                self._character_actions.append((folder, action))
            self._char_menu = char_menu  # 持有引用防回收

        menu.addAction(act_settings)
        menu.addAction(act_stats)
        menu.addSeparator()   # “退出”与其他功能分隔，固定在菜单最底部
        menu.addAction(act_quit)
        self._menu = menu  # 持有引用，防止被 Python 回收

        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setContextMenu(menu)
        self._tray.setToolTip("EyeGuard")
        self._tray.activated.connect(self._on_activated)

        agent.stateChanged.connect(self._on_state_changed)
        agent.workTick.connect(self._on_work_tick)

        self._tray.show()

    def sync_character(self, folder: str) -> None:
        """同步“切换角色”菜单的勾选状态。"""
        for name, action in self._character_actions:
            action.setChecked(name == folder)

    # ---------------- 对外 ----------------
    def hide(self) -> None:
        """隐藏托盘图标（程序退出前调用）。"""
        self._tray.hide()

    def show_message(self, title: str, body: str) -> None:
        """托盘气泡通知（“仅系统通知”提醒模式使用）。"""
        self._tray.showMessage(
            title, body, QSystemTrayIcon.MessageIcon.Information, 5000)

    def refresh(self) -> None:
        """立即刷新菜单文案与提示（配置变化后调用）。"""
        self._on_state_changed(self._agent.state)
        self._on_work_tick(self._agent.remaining_seconds())

    # ---------------- 内部 ----------------
    def _on_activated(self,
                      reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.settingsRequested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.restNowRequested.emit()

    def _on_state_changed(self, state: str) -> None:
        self._act_pause.setText(
            "恢复计时" if state == STATE_PAUSED else "暂停计时")
        if state == STATE_PAUSED:
            self._tray.setToolTip("EyeGuard · 计时已暂停")
        elif state == STATE_BREAK:
            self._tray.setToolTip("EyeGuard · 休息中…")
        else:
            self._on_work_tick(self._agent.remaining_seconds())

    def _on_work_tick(self, seconds: int) -> None:
        minutes, secs = divmod(max(0, seconds), 60)
        self._tray.setToolTip(f"EyeGuard · 下次休息 {minutes:02d}:{secs:02d}")
