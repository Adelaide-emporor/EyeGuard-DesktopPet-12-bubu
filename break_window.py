"""休息提醒窗口：全屏置顶遮罩 / 普通窗口两种形态 + 倒计时。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                               QProgressBar, QPushButton, QVBoxLayout,
                               QWidget)

from config import MODE_FULLSCREEN, MODE_WINDOW, AppConfig

_BUTTON_QSS = """
QPushButton {
    background-color: rgba(255, 255, 255, 0.12);
    color: #ECEFF1;
    border: 1px solid rgba(255, 255, 255, 0.28);
    border-radius: 8px;
    padding: 10px 24px;
    font-size: 15px;
}
QPushButton:hover { background-color: rgba(255, 255, 255, 0.22); }
QPushButton:pressed { background-color: rgba(255, 255, 255, 0.30); }
"""

_FULLSCREEN_QSS = """
#BreakRoot { background-color: rgba(13, 20, 34, 235); }
#titleLabel { color: #ECEFF1; font-size: 32px; font-weight: 700; }
#countdownLabel { color: #4DD0E1; font-size: 120px; font-weight: 800; }
#unitLabel { color: #90A4AE; font-size: 16px; }
#messageLabel { color: #CFD8DC; font-size: 20px; }
QProgressBar {
    background-color: rgba(255, 255, 255, 0.10);
    border: none; border-radius: 4px; max-height: 10px;
}
QProgressBar::chunk { background-color: #26A69A; border-radius: 4px; }
""" + _BUTTON_QSS

_WINDOW_QSS = """
#BreakRoot { background-color: #1B2430; }
#titleLabel { color: #ECEFF1; font-size: 26px; font-weight: 700; }
#countdownLabel { color: #4DD0E1; font-size: 96px; font-weight: 800; }
#unitLabel { color: #90A4AE; font-size: 14px; }
#messageLabel { color: #CFD8DC; font-size: 16px; }
QProgressBar {
    background-color: rgba(255, 255, 255, 0.10);
    border: none; border-radius: 4px; max-height: 8px;
}
QProgressBar::chunk { background-color: #26A69A; border-radius: 4px; }
""" + _BUTTON_QSS


class BreakWindow(QWidget):
    """休息倒计时窗口。

    结果信号（三选一，保证只发一次）：
        finished()  倒计时自然结束
        skipped()   用户跳过（按钮 / Esc / Alt+F4 关闭）
        postponed() 用户推迟
    """

    finished = Signal()
    skipped = Signal()
    postponed = Signal()
    closed = Signal()   # 窗口关闭（无论何种原因），供主程序释放展示权

    def __init__(self, cfg: AppConfig, is_test: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.is_test = is_test  # “测试弹窗”标记：离开自动结束时不计入统计
        self._remaining = cfg.break_seconds
        self._resolved = False  # 结果信号是否已发出
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)

        self.setObjectName("BreakRoot")
        self.setWindowTitle(cfg.popup_title)
        self._build_ui()
        self._apply_mode()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        self._title_label = QLabel(self._cfg.popup_title, self)
        self._title_label.setObjectName("titleLabel")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._countdown_label = QLabel(self._format_remaining(), self)
        self._countdown_label.setObjectName("countdownLabel")
        self._countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._unit_label = QLabel("秒后自动结束", self)
        self._unit_label.setObjectName("unitLabel")
        self._unit_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._message_label = QLabel(self._cfg.popup_message, self)
        self._message_label.setObjectName("messageLabel")
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_label.setWordWrap(True)

        self._progress = QProgressBar(self)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, max(1, self._cfg.break_seconds))

        btn_skip = QPushButton("跳过休息", self)
        btn_postpone = QPushButton(
            f"推迟 {self._cfg.postpone_minutes} 分钟", self)
        btn_skip.clicked.connect(lambda: self._resolve(self.skipped))
        btn_postpone.clicked.connect(lambda: self._resolve(self.postponed))

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn_skip)
        btn_row.addSpacing(16)
        btn_row.addWidget(btn_postpone)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 64, 48, 48)
        layout.setSpacing(18)
        layout.addStretch(1)
        layout.addWidget(self._title_label)
        layout.addWidget(self._countdown_label)
        layout.addWidget(self._unit_label)
        layout.addWidget(self._message_label)
        layout.addSpacing(12)
        layout.addWidget(self._progress)
        layout.addStretch(1)
        layout.addLayout(btn_row)

    def _apply_mode(self) -> None:
        """按配置设置窗口形态与样式。"""
        if self._cfg.popup_mode == MODE_FULLSCREEN:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setStyleSheet(_FULLSCREEN_QSS)
        else:  # MODE_WINDOW（notification 模式不会创建本窗口）
            flags = Qt.WindowType.Window
            if self._cfg.always_on_top:
                flags |= Qt.WindowType.WindowStaysOnTopHint
            self.setWindowFlags(flags)
            self.setStyleSheet(_WINDOW_QSS)

    # ---------------- 对外 ----------------
    def start(self) -> None:
        """显示窗口并开始倒计时。"""
        screen = QApplication.primaryScreen()
        if self._cfg.popup_mode == MODE_FULLSCREEN:
            if screen is not None:
                self.setGeometry(screen.geometry())  # 覆盖主显示器
            self.show()
        else:
            self.setFixedSize(640, 420)
            self.show()
            if screen is not None:
                frame = self.frameGeometry()
                frame.moveCenter(screen.availableGeometry().center())
                self.move(frame.topLeft())
        self.raise_()
        self.activateWindow()
        self._refresh()
        self._timer.start()

    def remaining(self) -> int:
        """剩余秒数。"""
        return self._remaining

    # ---------------- 内部 ----------------
    def _format_remaining(self) -> str:
        """60 秒以上显示 mm:ss，否则显示纯秒数。"""
        if self._remaining >= 60:
            return f"{self._remaining // 60:02d}:{self._remaining % 60:02d}"
        return str(self._remaining)

    def _refresh(self) -> None:
        self._countdown_label.setText(self._format_remaining())
        self._progress.setValue(self._remaining)
        self._unit_label.setText(
            "分:秒后自动结束" if self._remaining >= 60 else "秒后自动结束")

    def _on_tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._resolve(self.finished)
            return
        self._refresh()

    def _resolve(self, signal) -> None:
        """统一收口：保证结果信号最多发出一次，然后关闭窗口。"""
        if self._resolved:
            return
        self._resolved = True
        self._timer.stop()
        self.close()
        signal.emit()

    # ---------------- 事件 ----------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._resolve(self.skipped)
            return
        super().keyPressEvent(event)

    def dismiss_quietly(self) -> None:
        """立即关闭且不发任何结果信号（供单实例调度使用）。"""
        self._resolved = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer.stop()
        if not self._resolved:
            # Alt+F4 / 任务栏关闭等路径，视为跳过本次休息
            self._resolved = True
            self.skipped.emit()
        self.closed.emit()
        event.accept()
