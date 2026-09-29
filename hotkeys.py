"""全局快捷键：Ctrl+Alt+B 立即休息 / Ctrl+Alt+P 暂停-恢复计时。

经 RegisterHotKey 注册系统级热键（即使 EyeGuard 不在前台也生效），
WM_HOTKEY 消息经 QAbstractNativeEventFilter 转为 Qt 信号。
热键与已有软件冲突时注册失败：记录日志并回传失败列表，由主程序提示。
"""
from __future__ import annotations

import ctypes
import sys
from typing import Callable, List

from PySide6.QtCore import (QAbstractNativeEventFilter, QObject, Signal)
from PySide6.QtWidgets import QApplication, QWidget

from utils import LOG

IS_WINDOWS = sys.platform == "win32"

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_B = 0x42
VK_P = 0x50


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    """与 Win64 MSG 布局一致（与 activity.py 各自独立）。"""
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_uint64),
                ("lParam", ctypes.c_int64),
                ("time", ctypes.c_uint32),
                ("pt", _POINT)]


class _HotkeyFilter(QAbstractNativeEventFilter):
    """把 WM_HOTKEY 转发给 HotkeyManager。"""

    def __init__(self, callback: Callable[[int], bool]) -> None:
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message) -> bool:
        try:
            if event_type != b"windows_generic_MSG":
                return False
            msg = _MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                return self._callback(int(msg.wParam))
        except (ValueError, OSError, OverflowError):
            return False
        return False


class HotkeyManager(QObject):
    """注册/注销全局热键并发出信号。

    信号：
        restNow()       Ctrl+Alt+B —— 立即休息
        pauseToggled()  Ctrl+Alt+P —— 暂停/恢复计时
    """

    restNow = Signal()
    pauseToggled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.failed: List[str] = []   # 注册失败的热键名（冲突等）
        self._user32 = None
        self._widget: QWidget | None = None
        self._filter: _HotkeyFilter | None = None
        self._hwnd = 0
        if not IS_WINDOWS:
            LOG.info("非 Windows 平台，跳过全局快捷键注册")
            return
        try:
            self._user32 = ctypes.windll.user32
            self._widget = QWidget()
            self._widget.setObjectName("EyeGuardHotkeyWindow")
            self._hwnd = int(self._widget.winId())
            mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
            self._register(1, mods, VK_B, "Ctrl+Alt+B 立即休息")
            self._register(2, mods, VK_P, "Ctrl+Alt+P 暂停/恢复")
            self._filter = _HotkeyFilter(self._on_hotkey)
            app = QApplication.instance()
            if app is not None:
                app.installNativeEventFilter(self._filter)
        except (OSError, ValueError, OverflowError, RuntimeError) as exc:
            LOG.warning("全局快捷键初始化失败: %s", exc)

    def _register(self, hotkey_id: int, mods: int, vk: int,
                  name: str) -> None:
        assert self._user32 is not None
        if self._user32.RegisterHotKey(self._hwnd, hotkey_id, mods, vk):
            LOG.info("全局快捷键已注册: %s", name)
        else:
            self.failed.append(name)
            LOG.warning("全局快捷键注册失败（可能被占用）: %s", name)

    def _on_hotkey(self, hotkey_id: int) -> bool:
        if hotkey_id == 1:
            self.restNow.emit()
            return True
        if hotkey_id == 2:
            self.pauseToggled.emit()
            return True
        return False

    def shutdown(self) -> None:
        """注销全部热键并移除过滤器（程序退出前调用）。"""
        if not IS_WINDOWS or self._user32 is None:
            return
        try:
            self._user32.UnregisterHotKey(self._hwnd, 1)
            self._user32.UnregisterHotKey(self._hwnd, 2)
            if self._filter is not None:
                app = QApplication.instance()
                if app is not None:
                    app.removeNativeEventFilter(self._filter)
            LOG.info("全局快捷键已注销")
        except OSError as exc:
            LOG.debug("注销全局快捷键失败: %s", exc)
