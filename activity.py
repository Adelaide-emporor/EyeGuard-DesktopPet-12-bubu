"""屏幕活跃度监控：判断用户是否正在实际使用屏幕（EyeGuard 计时的依据）。

以下状态视为“离开”，计时暂停；恢复使用时回传离开时长与原因：
锁屏、屏幕保护程序、显示器关闭、系统睡眠/休眠、会话断开/切换、键鼠空闲。

Windows 10/11 实现手段：
- WM_WTSSESSION_CHANGE（wtsapi32.WTSRegisterSessionNotification）
    → 锁屏/解锁、控制台与远程会话断开/接入
- WM_POWERBROADCAST
    → 睡眠 PBT_APMSUSPEND / 唤醒 PBT_APMRESUMEAUTOMATIC
- RegisterPowerSettingNotification(GUID_CONSOLE_DISPLAY_STATE)
    → 显示器关闭/点亮
- SPI_GETSCREENSAVERRUNNING 每秒轮询 → 屏幕保护程序
- GetLastInputInfo → 键鼠空闲（辅助信号；看视频无输入也会被视为空闲）

任一通知注册失败时自动降级为轮询兜底，绝不影响主程序运行。
非 Windows 平台退化为“始终活跃”（本项目目标平台为 Windows）。
"""
from __future__ import annotations

import ctypes
import sys
import uuid
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from PySide6.QtCore import (QAbstractNativeEventFilter, QObject, QTimer,
                            Signal)
from PySide6.QtWidgets import QApplication, QWidget

from utils import LOG

IS_WINDOWS = sys.platform == "win32"

# ---------------- 行为常量（可按需调整后重新打包） ----------------
# 键鼠空闲达到该秒数视为“短暂离开”：暂停计时，恢复后继续剩余时间。
# 设为 None 可禁用空闲检测（只依赖锁屏/屏保/关屏/睡眠/会话事件）。
IDLE_PAUSE_SECONDS: Optional[float] = 30.0
# 离开总时长超过该秒数，恢复时视为眼睛已休息，重置整个工作周期。
AWAY_RESET_SECONDS: float = 60.0
# 短暂离开恢复后，剩余时间若不足该值则补足，避免一回来就弹提醒。
RESUME_GRACE_SECONDS: int = 60

# ---------------- 离开原因 ----------------
REASON_LOCK = "lock"                # 锁屏
REASON_SCREENSAVER = "screensaver"  # 屏幕保护程序
REASON_DISPLAY = "display"          # 显示器关闭
REASON_SLEEP = "sleep"              # 睡眠/休眠
REASON_SESSION = "session"          # 会话断开/切换
REASON_IDLE = "idle"                # 键鼠空闲

REASON_TEXT: Dict[str, str] = {
    REASON_LOCK: "锁屏",
    REASON_SCREENSAVER: "屏幕保护程序",
    REASON_DISPLAY: "显示器关闭",
    REASON_SLEEP: "系统睡眠/休眠",
    REASON_SESSION: "会话断开/切换",
    REASON_IDLE: "键鼠空闲",
}
# 经历过这些状态即视为“眼睛已休息”（键鼠空闲不算）；
# 同时也是“离开屏幕”的判定依据（久坐提醒据此暂停并重置周期）
BIG_REASONS = {REASON_LOCK, REASON_SCREENSAVER, REASON_DISPLAY,
               REASON_SLEEP, REASON_SESSION}

# ---------------- Win32 常量与结构 ----------------
if IS_WINDOWS:
    WM_POWERBROADCAST = 0x0218
    WM_WTSSESSION_CHANGE = 0x02B1
    PBT_APMSUSPEND = 0x0004
    PBT_APMRESUMEAUTOMATIC = 0x0012
    PBT_APMRESUMESUSPEND = 0x0013
    PBT_POWERSETTINGCHANGE = 0x8013
    SPI_GETSCREENSAVERRUNNING = 0x0072
    DEVICE_NOTIFY_WINDOW_HANDLE = 0x0
    NOTIFY_FOR_THIS_SESSION = 0x0
    MAXIMUM_ALLOWED = 0x02000000
    WTS_CONSOLE_CONNECT = 0x1
    WTS_REMOTE_CONNECT = 0x2
    WTS_CONSOLE_DISCONNECT = 0x3
    WTS_SESSION_LOCK = 0x7
    WTS_SESSION_UNLOCK = 0x8
    WTS_REMOTE_DISCONNECT = 0x9
    # 显示器电源状态数据：0=关闭，1=打开，2=变暗（按打开处理）
    GUID_CONSOLE_DISPLAY_STATE = uuid.UUID(
        "6FE69556-704A-47A0-8F24-C28D936FDA47")

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _MSG(ctypes.Structure):
        """与 Win64 MSG 布局一致（ctypes 按对齐规则自动补 padding）。"""
        _fields_ = [("hwnd", ctypes.c_void_p),
                    ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_uint64),
                    ("lParam", ctypes.c_int64),
                    ("time", ctypes.c_uint32),
                    ("pt", _POINT)]

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32),
                    ("dwTime", ctypes.c_uint32)]

    class _POWERBROADCAST_SETTING(ctypes.Structure):
        _fields_ = [("Guid", ctypes.c_ubyte * 16),
                    ("DataLength", ctypes.c_uint32),
                    ("Data", ctypes.c_ubyte * 8)]


class _WinEventFilter(QAbstractNativeEventFilter):
    """把隐藏窗口收到的电源/会话广播转发给 ScreenActivityMonitor。"""

    def __init__(self, hwnd: int,
                 callback: Callable[[int, int, int], bool]) -> None:
        super().__init__()
        self._hwnd = hwnd
        self._callback = callback

    def nativeEventFilter(self, event_type, message) -> bool:
        try:
            if event_type != b"windows_generic_MSG" or not self._hwnd:
                return False
            msg = _MSG.from_address(int(message))
            if msg.hwnd is None or int(msg.hwnd) != self._hwnd:
                return False
            if msg.message in (WM_POWERBROADCAST, WM_WTSSESSION_CHANGE):
                return self._callback(msg.message, int(msg.wParam),
                                      int(msg.lParam))
        except (ValueError, OSError, OverflowError):
            return False
        return False


class ScreenActivityMonitor(QObject):
    """屏幕活跃状态机（运行于 GUI 线程）。

    信号：
        paused(reason)              进入离开状态，reason 为 REASON_* 常量
        resumed(away_seconds, big)  回到屏幕；big 表示期间经历过锁屏/屏保/
                                    关屏/睡眠/会话断开之一（应视为已休息）
    """

    paused = Signal(str)
    resumed = Signal(float, bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._active = True
        self._absence_start: Optional[datetime] = None  # 本次离开的起点
        self._big_state = False                          # 本次离开是否经历过大状态
        self._flags: Dict[str, bool] = {reason: False for reason in
                                        (REASON_LOCK, REASON_SCREENSAVER,
                                         REASON_DISPLAY, REASON_SLEEP,
                                         REASON_SESSION, REASON_IDLE)}
        self._widget: Optional[QWidget] = None
        self._filter: Optional[_WinEventFilter] = None
        self._guid_buf = None
        self._notify_power = None
        self._wts_ok = False
        self._hwnd = 0
        self._user32 = None
        self._wtsapi32 = None
        self._kernel32 = None
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_once)
        if IS_WINDOWS:
            self._setup_native()
            self._poll.start()
        LOG.info("屏幕活跃度监控已启动（%s）",
                 "Win32 事件 + 每秒轮询" if IS_WINDOWS else "非 Windows，始终活跃")

    # ---------------- 对外 ----------------
    @property
    def is_active(self) -> bool:
        """用户当前是否正在使用屏幕（键鼠空闲也算离开）。"""
        return self._active

    @property
    def screen_present(self) -> bool:
        """屏幕是否可用：未锁屏、无屏保、显示器开启、未睡眠、会话已连接。

        键鼠空闲不影响该值——看视频同样属于“坐在屏幕前”，久坐时间照常累计。
        """
        return not any(on for name, on in self._flags.items()
                       if name != REASON_IDLE)

    def shutdown(self) -> None:
        """停止轮询并反注册系统通知（程序退出前调用）。"""
        self._poll.stop()
        if not IS_WINDOWS or self._user32 is None:
            return
        try:
            if self._notify_power:
                self._user32.UnregisterPowerSettingNotification(
                    self._notify_power)
            if self._wts_ok and self._wtsapi32 is not None:
                self._wtsapi32.WTSUnRegisterSessionNotification(self._hwnd)
            if self._filter is not None:
                app = QApplication.instance()
                if app is not None:
                    app.removeNativeEventFilter(self._filter)
        except OSError as exc:
            LOG.debug("屏幕监控反注册失败: %s", exc)

    def idle_seconds(self) -> Optional[float]:
        """距上次键鼠输入的秒数；获取失败返回 None。"""
        if not IS_WINDOWS or self._user32 is None:
            return None
        try:
            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not self._user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            now32 = self._kernel32.GetTickCount()
            return ((now32 - info.dwTime) & 0xFFFFFFFF) / 1000.0
        except (OSError, ValueError):
            return None

    # ---------------- 原生注册 ----------------
    def _setup_native(self) -> None:
        """创建隐藏原生窗口并注册系统通知；任何失败都降级为纯轮询。"""
        try:
            self._user32 = ctypes.windll.user32
            self._wtsapi32 = ctypes.windll.wtsapi32
            self._kernel32 = ctypes.windll.kernel32
        except (OSError, AttributeError) as exc:
            LOG.warning("加载 Win32 API 失败，仅剩轮询检测: %s", exc)
            return
        self._user32.SystemParametersInfoW.argtypes = (
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint)
        self._user32.SystemParametersInfoW.restype = ctypes.c_int
        self._user32.GetLastInputInfo.argtypes = (
            ctypes.POINTER(_LASTINPUTINFO),)
        self._user32.GetLastInputInfo.restype = ctypes.c_int
        self._user32.RegisterPowerSettingNotification.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        self._user32.RegisterPowerSettingNotification.restype = ctypes.c_void_p
        self._user32.UnregisterPowerSettingNotification.argtypes = (
            ctypes.c_void_p,)
        self._user32.UnregisterPowerSettingNotification.restype = ctypes.c_int
        self._user32.OpenInputDesktop.argtypes = (
            ctypes.c_uint, ctypes.c_int, ctypes.c_uint)
        self._user32.OpenInputDesktop.restype = ctypes.c_void_p
        self._wtsapi32.WTSRegisterSessionNotification.argtypes = (
            ctypes.c_void_p, ctypes.c_uint)
        self._wtsapi32.WTSRegisterSessionNotification.restype = ctypes.c_int
        self._wtsapi32.WTSUnRegisterSessionNotification.argtypes = (
            ctypes.c_void_p,)
        self._kernel32.GetTickCount.restype = ctypes.c_uint32
        try:
            # CloseDesktop 导出自 user32（不是 kernel32）
            self._user32.CloseDesktop.argtypes = (ctypes.c_void_p,)
            # 隐藏的原生顶层窗口：系统广播（电源/会话）会投递到它的消息队列
            widget = QWidget()
            widget.setObjectName("EyeGuardActivityWindow")
            self._widget = widget
            self._hwnd = int(widget.winId())
            self._filter = _WinEventFilter(self._hwnd, self._on_native_message)
            app = QApplication.instance()
            if app is not None:
                app.installNativeEventFilter(self._filter)
            self._wts_ok = bool(
                self._wtsapi32.WTSRegisterSessionNotification(
                    self._hwnd, NOTIFY_FOR_THIS_SESSION))
            if not self._wts_ok:
                LOG.warning("WTS 会话通知注册失败，锁屏改用 OpenInputDesktop 轮询兜底")
            guid_buf = ctypes.create_string_buffer(
                GUID_CONSOLE_DISPLAY_STATE.bytes_le, 16)
            self._guid_buf = guid_buf  # 持有引用，防止 GUID 内存被回收
            handle = self._user32.RegisterPowerSettingNotification(
                self._hwnd, ctypes.cast(guid_buf, ctypes.c_void_p),
                DEVICE_NOTIFY_WINDOW_HANDLE)
            self._notify_power = handle or None
            if not self._notify_power:
                LOG.warning("显示器电源状态通知注册失败，关屏检测将依赖睡眠/空闲事件")
        except (OSError, ValueError, OverflowError, RuntimeError) as exc:
            LOG.warning("屏幕活跃度原生监控初始化失败，降级为纯轮询: %s", exc)

    # ---------------- 原生消息 ----------------
    def _on_native_message(self, message: int, wparam: int,
                           lparam: int) -> bool:
        """处理发往隐藏窗口的 WM_POWERBROADCAST / WM_WTSSESSION_CHANGE。"""
        if message == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK:
                self._apply_flag(REASON_LOCK, True)
            elif wparam == WTS_SESSION_UNLOCK:
                self._apply_flag(REASON_LOCK, False)
            elif wparam in (WTS_CONSOLE_DISCONNECT, WTS_REMOTE_DISCONNECT):
                self._apply_flag(REASON_SESSION, True)
            elif wparam in (WTS_CONSOLE_CONNECT, WTS_REMOTE_CONNECT):
                self._apply_flag(REASON_SESSION, False)
            return True
        if message == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                self._apply_flag(REASON_SLEEP, True)
            elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                self._apply_flag(REASON_SLEEP, False)
            elif wparam == PBT_POWERSETTINGCHANGE:
                self._on_power_setting(lparam)
            return True
        return False

    def _on_power_setting(self, lparam: int) -> None:
        """解析 GUID_CONSOLE_DISPLAY_STATE 的显示器开关状态。"""
        try:
            setting = _POWERBROADCAST_SETTING.from_address(lparam)
            if bytes(setting.Guid) != GUID_CONSOLE_DISPLAY_STATE.bytes_le:
                return
            if setting.DataLength < 4:
                return
            state = int.from_bytes(bytes(setting.Data[:4]), "little")
        except (ValueError, OSError):
            return
        self._apply_flag(REASON_DISPLAY, state == 0)

    # ---------------- 每秒轮询 ----------------
    def _poll_once(self) -> None:
        """轮询屏幕保护程序、（必要时）锁屏兜底与键鼠空闲。"""
        if not IS_WINDOWS or self._user32 is None:
            return  # Win32 API 加载失败或非 Windows：保持始终活跃
        self._poll_screensaver()
        if not self._wts_ok:
            self._poll_lock_fallback()
        self._poll_idle()

    def _poll_screensaver(self) -> None:
        """SPI_GETSCREENSAVERRUNNING 查询屏幕保护程序是否正在运行。"""
        flag = ctypes.c_int(0)
        ok = self._user32.SystemParametersInfoW(
            SPI_GETSCREENSAVERRUNNING, 0, ctypes.addressof(flag), 0)
        if ok:
            self._apply_flag(REASON_SCREENSAVER, bool(flag.value))

    def _poll_lock_fallback(self) -> None:
        """WTS 注册失败的兜底：打不开输入桌面通常意味着锁屏/安全桌面。"""
        try:
            handle = self._user32.OpenInputDesktop(0, False, MAXIMUM_ALLOWED)
        except OSError:
            return
        locked = handle is None
        if handle:
            self._user32.CloseDesktop(handle)
        self._apply_flag(REASON_LOCK, locked)

    def _poll_idle(self) -> None:
        """键鼠空闲辅助信号（看视频无输入也会视为空闲离开）。"""
        if IDLE_PAUSE_SECONDS is None:
            return
        idle = self.idle_seconds()
        if idle is None:
            return
        self._apply_flag(REASON_IDLE, idle >= IDLE_PAUSE_SECONDS)

    # ---------------- 状态机 ----------------
    def _apply_flag(self, reason: str, on: bool) -> None:
        """更新单个离开标志并重算活跃状态；值未变化时不做任何事。"""
        if self._flags.get(reason) == on:
            return
        self._flags[reason] = on
        if on and reason in BIG_REASONS:
            self._big_state = True
        LOG.debug("系统状态变化：%s -> %s", REASON_TEXT.get(reason, reason),
                  "进入" if on else "退出")
        self._refresh()

    def _refresh(self) -> None:
        """根据所有标志重算活跃/离开，并在状态翻转时发信号。"""
        inactive = any(self._flags.values())
        if self._active and inactive:
            reason = next(name for name, on in self._flags.items() if on)
            self._active = False
            if reason == REASON_IDLE:
                # 空闲离开的起点是“最后一次键鼠输入”的时刻
                idle = self.idle_seconds() or 0.0
                self._absence_start = datetime.now() - timedelta(seconds=idle)
            else:
                self._absence_start = datetime.now()
            self.paused.emit(reason)
        elif not self._active and not inactive:
            away = 0.0
            if self._absence_start is not None:
                away = (datetime.now() - self._absence_start).total_seconds()
            big = self._big_state
            self._big_state = False
            self._absence_start = None
            self._active = True
            self.resumed.emit(away, big)
