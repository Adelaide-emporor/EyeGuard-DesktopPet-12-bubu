"""桌宠窗口：基于角色包帧序列的动画渲染（一二 / 布布 / 用户自装角色包）。

三类窗口：
- SedentaryPetWindow  起身提醒：右下角跳出（jump）→ 挥手（wave）+ 气泡
  “该站起来动动啦！”与“已起身/稍后再提醒”按钮；确认后播放 happy 再退场。
- SitDownPetWindow    坐下提示：跳出 → 乖巧坐着（sit）+ 气泡“可以坐啦~”，
  点击任意处或 15 秒后自动消失。
- ResidentPetWindow   常驻待机：安静待在右下角播放 idle（眨眼/呼吸），
  鼠标点击穿透，不遮挡任何操作。

角色帧来自 CharacterPack（characters/ 角色包），窗口只负责按时序播放。
"""
from __future__ import annotations

import math
import random
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QRect, QRectF,
                            Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QBrush, QColor, QCloseEvent, QPainter,
                           QPainterPath, QPixmap, QPen)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from character import CharacterPack
from config import AppConfig

BUBBLE_W = 300
PET_DISPLAY_H = 150    # 所有桌宠统一显示高度（像素），不随素材尺寸变化

# 摸头/闲聊台词（随机冒一句，出现在桌宠头顶的临时气泡里）
CHATTER_LINES = [
    "喝水了吗？", "记得看看远处哦~", "坐直一点更舒服！",
    "伸个懒腰吧~", "我一直在陪你哦", "起来走两步吧！",
]
_LABEL_QSS = ("color:#4E342E; font-size:15px; font-weight:600;"
              " background:transparent;")
_BTN_GREEN_QSS = ("QPushButton { background-color:#2E7D32; color:white;"
                  " border:none; border-radius:6px; padding:6px 18px;"
                  " font-size:13px; }"
                  "QPushButton:hover { background-color:#388E3C; }")
_BTN_GREY_QSS = ("QPushButton { background-color:#607D8B; color:white;"
                 " border:none; border-radius:6px; padding:6px 18px;"
                 " font-size:13px; }"
                 "QPushButton:hover { background-color:#78909C; }")


def _tail_path(x: float, y: float) -> QPainterPath:
    """气泡尾巴三角（开放路径：填充视为闭合，描边只画两条斜边）。"""
    path = QPainterPath()
    path.moveTo(x, y)
    path.lineTo(x + 14.0, y + 13.0)
    path.lineTo(x + 28.0, y)
    return path


class _BubblePanel(QWidget):
    """白色圆角对话气泡，尾巴指向角色。

    buttons：[(按钮文字, 回调, 样式), ...]；None 表示无按钮气泡。
    """

    def __init__(self, text: str,
                 buttons: Optional[List[Tuple[str, Callable[[], None], str]]],
                 tail_x: float, height: int, parent: QWidget) -> None:
        super().__init__(parent)
        self._tail_x = tail_x
        self.setFixedSize(BUBBLE_W - 10, height)

        self._message = QLabel(text, self)
        self._message.setStyleSheet(_LABEL_QSS)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.addWidget(self._message)
        if buttons:  # 所有按钮排在同一行
            row = QHBoxLayout()
            row.addStretch(1)
            for i, (label, callback, qss) in enumerate(buttons):
                btn = QPushButton(label, self)
                btn.setStyleSheet(qss)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _c=False, cb=callback: cb())
                row.addWidget(btn)
                if i < len(buttons) - 1:
                    row.addSpacing(12)
            row.addStretch(1)
            layout.addLayout(row)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        pen = QPen(QColor("#3E2B21"), 2)
        body = QRectF(1.0, 1.0, w - 2.0, h - 17.0)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 248)))
        painter.drawRoundedRect(body, 16.0, 16.0)
        tx = min(max(self._tail_x, 30.0), w - 40.0)
        ty = h - 16.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 248)))
        painter.drawPath(_tail_path(tx, ty))
        painter.setPen(pen)
        painter.drawLine(QPointF(tx + 1.0, ty - 1.0),
                         QPointF(tx + 14.0, ty + 12.0))
        painter.drawLine(QPointF(tx + 14.0, ty + 12.0),
                         QPointF(tx + 27.0, ty - 1.0))


class PetWindow(QWidget):
    """桌宠窗口基类：按角色包帧序列播放动画。

    - mode="reminder"：出场 jump → 循环 wave，气泡含“已起身/稍后再提醒”。
      confirm() 发出 confirmed 并播放 happy，随后滑出退场；
      snooze() 发出 snoozed 并滑出退场；超时/关闭未处理按 snoozed。
    - mode="sit"：出场 jump → 循环 sit，气泡“可以坐啦~”，点击任意处关闭。
    - mode="resident"：常驻右下角循环 idle，鼠标点击穿透。
    """

    confirmed = Signal()
    snoozed = Signal()
    skipClicked = Signal()        # 用户选择“跳过休息”（含双击退下）
    postponeClicked = Signal()    # 用户选择“N 分钟后提醒”
    closed = Signal()   # 窗口关闭（无论何种原因），供主程序调度常驻桌宠回归
    homeMoved = Signal(int, int)  # 常驻桌宠被拖拽到新位置 (x, y)
    petted = Signal()   # 用户单击摸头（常驻桌宠）

    def __init__(self, pack: CharacterPack, mode: str,
                 is_test: bool = False, auto_close_ms: Optional[int] = None,
                 bubble_text: Optional[str] = None, pet_name: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pack = pack
        self._mode = mode
        self.is_test = is_test
        self._resolved = False
        self._tick = 0
        self._phase = "enter" if mode in ("reminder", "sit") else "main"
        self._phase_start = 0  # 当前相位开始的 tick
        self._bubble_text = bubble_text
        self._pet_name = pet_name
        self._name_h = 26 if pet_name else 0
        self._drag_offset: Optional[QPoint] = None
        self._press_global: Optional[QPoint] = None
        self._drag_moved = False
        self._pet_until_tick = 0   # 摸头后保持 happy 动作直到该 tick
        self._chatter: Optional["ChatterBubble"] = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._bubble: Optional[_BubblePanel] = None
        self._build_frames()
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(max(30, 1000 // max(1, pack.fps)))
        self._timer.timeout.connect(self._on_tick)
        if auto_close_ms is not None:
            QTimer.singleShot(auto_close_ms, self.close)

    # ---------------- 帧与尺寸 ----------------
    def _build_frames(self) -> None:
        """生成显示用素材：single 型缩放整图；frames 型缩放每帧。"""
        self._interval_ms = max(30, 1000 // max(1, self._pack.fps))
        self._scaled: Dict[str, List[QPixmap]] = {}
        self._base: Optional[QPixmap] = None
        if self._pack.kind == "single" and self._pack.image is not None:
            img = self._pack.image
            ratio = PET_DISPLAY_H / img.height()  # 统一显示高度
            self._base = img.scaled(
                max(1, round(img.width() * ratio)), PET_DISPLAY_H,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            return
        for state, frames in self._pack.states.items():
            if not frames:
                continue
            ratio = PET_DISPLAY_H / frames[0].height()
            self._scaled[state] = [
                frame.scaled(
                    max(1, round(frame.width() * ratio)), PET_DISPLAY_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                for frame in frames]

    def _build_ui(self) -> None:
        if self._pack.kind == "single" and self._base is not None:
            pet_w, pet_h = self._base.width(), self._base.height()
        else:
            frames = self._pack.frames("idle")
            ref = frames[0] if frames else None
            ref_w = ref.width() if ref else 190
            ref_h = ref.height() if ref else PET_DISPLAY_H
            pet_w = int(ref_w * PET_DISPLAY_H / ref_h)
            pet_h = PET_DISPLAY_H
        self._name_label: Optional[QLabel] = None
        if self._pet_name:
            self._name_label = QLabel(self._pet_name, self)
            self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._name_label.setStyleSheet(
                "color:#FFFFFF; background:rgba(30,42,54,215);"
                "border-radius:10px; font-size:13px; font-weight:600;"
                "padding:1px 8px;")
        if self._mode == "resident":
            self._bubble = None
            self.setFixedSize(pet_w + 6, pet_h + self._name_h + 8)
        else:
            buttons: Optional[List[Tuple[str, Callable[[], None], str]]] = None
            default_text = "眺望远处，休息一下眼睛~"
            if self._mode == "reminder":
                buttons = [("已起身", self.confirm, _BTN_GREEN_QSS),
                           ("稍后再提醒", self.snooze, _BTN_GREY_QSS)]
                default_text = "该站起来动动啦！"
            elif self._mode == "sit":
                default_text = "可以坐啦~"
            elif self._mode == "break":
                buttons = [("跳过休息", self._skip_break, _BTN_GREY_QSS),
                           (getattr(self, "_eye_snooze_label", "5 分钟后提醒"),
                            self._eye_snooze, _BTN_GREEN_QSS)]
                default_text = "眺望远处，休息一下眼睛~"
            bubble_height = (118 if self._mode == "reminder"
                             else 132 if self._mode == "break" else 96)
            self._bubble = _BubblePanel(
                self._bubble_text or default_text,
                buttons, BUBBLE_W * 0.62, bubble_height, self)
            self._bubble.move(5, 0)
            self.setFixedSize(max(BUBBLE_W, pet_w + 16),
                              self._bubble.height() + pet_h
                              + self._name_h + 10)
        if self._name_label is not None:
            self._name_label.setGeometry(
                3, self.height() - self._name_h - 2,
                self.width() - 6, self._name_h)
            self._name_label.raise_()

    def set_name(self, name: str) -> None:
        """更新桌宠名字（显示在角色下方，不遮挡人物）。"""
        self._pet_name = name
        self._name_h = 26 if name else 0
        if self._name_label is not None:
            self._name_label.setText(name)
        self._build_ui()

    def set_pack(self, pack: CharacterPack) -> None:
        """切换角色包（立即生效，常驻窗口复用）。"""
        self._pack = pack
        self._build_frames()
        self._timer.setInterval(max(30, 1000 // max(1, pack.fps)))
        if self._mode == "resident":
            self._build_ui()
        self.update()

    # ---------------- 显示与退场 ----------------
    def start(self) -> None:
        """显示窗口；提醒类从屏幕右下角滑入，常驻直接落位。"""
        screen = QApplication.primaryScreen()
        geo = (screen.availableGeometry() if screen is not None
               else QRect(0, 0, 1920, 1080))
        target_x = geo.right() - self.width() - 8
        target_y = geo.bottom() - self.height() - 8
        self.show()
        if self._mode == "resident":
            self.move(target_x, target_y)
        else:
            self.move(target_x, geo.bottom() + 10)
            self._anim = QVariantAnimation(self)
            self._anim.setStartValue(float(geo.bottom() + 10))
            self._anim.setEndValue(float(target_y))
            self._anim.setDuration(650)
            self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
            self._anim.valueChanged.connect(
                lambda y: self.move(target_x, int(y)))
            self._anim.finished.connect(
                lambda: self.move(target_x, target_y))
            self._anim.start()
        self.raise_()
        if self._mode != "resident":
            self.activateWindow()
        self._timer.start()

    def _slide_out(self) -> None:
        """动作播放完毕后向屏幕右下角外滑出，结束后关闭窗口。"""
        screen = QApplication.primaryScreen()
        bottom = (screen.availableGeometry().bottom() + 20
                  if screen is not None else self.y() + 300)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(float(self.y()))
        self._anim.setEndValue(float(bottom))
        self._anim.setDuration(420)
        self._anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim.valueChanged.connect(lambda y: self.move(self.x(), int(y)))
        self._anim.finished.connect(self.close)
        self._anim.start()

    # ---------------- 状态机 ----------------
    def _on_tick(self) -> None:
        self._tick += 1
        if self._phase == "enter" and self._mode in ("reminder", "sit"):
            if self._tick * self._interval_ms >= self._pack.duration_ms("jump"):
                self._phase = "main"
                self._phase_start = self._tick
        elif self._phase == "happy":
            if (self._tick - self._phase_start) * self._interval_ms >= \
                    self._pack.duration_ms("happy"):
                self._timer.stop()
                self._slide_out()
        self.update()

    def _current_state(self) -> str:
        """按窗口模式与阶段推导当前动作。"""
        if self._mode == "resident":
            return ("happy" if self._tick < self._pet_until_tick else "idle")
        if self._mode == "break":
            return "blink"
        if self._phase == "enter":
            return "jump"
        if self._phase == "happy":
            return "happy"
        return "wave" if self._mode == "reminder" else "sit"

    def _current_frame(self) -> Optional[QPixmap]:
        if self._pack.kind == "single":
            return None  # single 型由 _paint_single 变换绘制
        state = self._current_state()
        frames = self._scaled.get(state) or self._scaled.get("idle") or []
        if not frames:
            return None
        if state == "jump":
            index = min(self._tick, len(frames) - 1)
        elif state == "happy":
            index = min(self._tick - self._phase_start, len(frames) - 1)
        else:
            index = self._tick % len(frames)
        return frames[index]

    def _paint_single(self, painter: QPainter, anchor_y: float) -> None:
        """single 型：原图只做位移/摆动/呼吸/眨眼等物理变换，笔画绝不变形。"""
        state = self._current_state()
        anim = self._pack.anim("idle" if state == "blink" else state)
        t_ms = self._tick * self._interval_ms
        period = max(200, int(anim.get("period_ms", 2400)))
        phase = 2 * math.pi * ((t_ms % period) / period)
        bob = float(anim.get("bob", 0.0)) * math.sin(phase)
        sway = float(anim.get("sway_deg", 0.0)) * math.sin(phase)
        hop_px = float(anim.get("hop", 0.0))
        hop = 0.0
        if hop_px > 0:
            hop_period = max(200, int(anim.get("hop_period_ms", 640)))
            hop = hop_px * abs(math.sin(math.pi * (t_ms % hop_period)
                                        / hop_period))
        breath = 1.0 + 0.012 * math.sin(2 * math.pi *
                                        ((t_ms % max(period, 1200))
                                         / max(period, 1200)))
        squash = 1.0
        if state == "blink":
            # 眨眼：闭眼→睁眼循环（整图 scaleY 物理变换，模拟眯眼）
            cycle = t_ms % 1200
            if cycle < 320:
                squash = 1.0 - 0.09 * math.sin(math.pi * cycle / 320)
        base = self._base
        if base is None:
            return
        painter.save()
        painter.translate(self.width() / 2.0, anchor_y - bob - hop)
        painter.rotate(sway)
        painter.scale(breath, breath * squash)
        painter.drawPixmap(QPointF(-base.width() / 2.0, -float(base.height())),
                           base)
        # 手动框选的眨眼区域：眨眼脉冲期间以肤色遮罩短暂覆盖（模拟闭眼）
        if (state == "blink" and squash < 1.0
                and self._pack.kind == "single" and self._pack.eye_rects):
            img_w, img_h = self._pack.image.width(), self._pack.image.height()
            sx = base.width() / img_w
            sy = base.height() / img_h
            origin_x = -base.width() / 2.0
            origin_y = -float(base.height())
            painter.setPen(Qt.PenStyle.NoPen)
            for (rx, ry, rw, rh), color in zip(
                    self._pack.eye_rects,
                    self._pack.eye_colors or ["#E8BFA8"] * len(
                        self._pack.eye_rects)):
                painter.setBrush(QColor(color))
                painter.drawEllipse(QRectF(origin_x + rx * sx,
                                           origin_y + ry * sy,
                                           rw * sx, rh * sy))
        painter.restore()

    def _skip_break(self) -> None:
        """跳过当前提醒（按钮或双击）：通知主程序后立即退场。"""
        if self._resolved:
            return
        self._resolved = True
        self._timer.stop()
        self.skipClicked.emit()
        self.close()

    def _eye_snooze(self) -> None:
        """护眼提醒“N 分钟后提醒”：通知主程序后退场。"""
        if self._resolved:
            return
        self._resolved = True
        self._timer.stop()
        self.postponeClicked.emit()
        self.close()

    def mouseDoubleClickEvent(self, event) -> None:
        if self._mode == "break":
            self._skip_break()  # 双击退下 = 跳过本次护眼提醒
        event.accept()

    def confirm(self) -> None:
        """“已起身”：立即发信号（站立倒计时即刻开始），角色开心退场。"""
        if self._resolved:
            return
        self._resolved = True
        self._phase = "happy"
        self._phase_start = self._tick
        self.confirmed.emit()

    def snooze(self) -> None:
        """“稍后再提醒”或点击角色关闭。"""
        if self._resolved:
            return
        self._resolved = True
        self.snoozed.emit()
        self._slide_out()

    def dismiss_quietly(self) -> None:
        """立即销毁窗口且不发任何结果信号（供“同时只保留一个实例”调度）。"""
        self._resolved = True
        self._timer.stop()
        self.close()

    # ---------------- 事件与绘制 ----------------
    def mousePressEvent(self, event) -> None:
        """所有桌宠窗口都支持按住拖动；原地单击再按模式执行原有行为。"""
        if event.button() == Qt.MouseButton.LeftButton:
            gp = event.globalPosition().toPoint()
            self._press_global = gp
            self._drag_offset = gp - self.frameGeometry().topLeft()
            self._drag_moved = False
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is None:
            event.accept()
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if not self._drag_moved and delta.manhattanLength() > 8:
            self._drag_moved = True  # 超过阈值视为拖拽，而不是单击
        if self._drag_moved and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._press_global is not None:
            pos = (event.position().toPoint()
                   if hasattr(event, "position") else event.pos())
            inside_bubble = (self._bubble is not None
                             and self._bubble.geometry().contains(pos))
            if self._drag_moved:
                if self._mode == "resident":  # 只有常驻桌宠持久化位置
                    new_pos = self._clamp_into_screen(self.pos())
                    self.move(new_pos)
                    self.homeMoved.emit(new_pos.x(), new_pos.y())
            elif self._mode == "resident":
                self._on_petted()  # 原地单击常驻桌宠 = 摸头互动
            else:  # 原地单击（未拖动）→ 执行原有单击行为
                if self._mode == "sit" and not inside_bubble:
                    self._resolved = True
                    self._timer.stop()
                    self.close()
                elif (self._mode == "reminder" and not inside_bubble
                      and not self._resolved):
                    self.snooze()
        self._press_global = None
        self._drag_moved = False
        event.accept()

    def _on_petted(self) -> None:
        """摸头互动：开心跳一下，偶尔回一句话（主程序负责音效）。"""
        duration = self._pack.duration_ms("happy")
        self._pet_until_tick = self._tick + max(
            1, duration // self._interval_ms)
        self.petted.emit()
        if random.random() < 0.6:
            self.show_chatter(random.choice(CHATTER_LINES))

    def show_chatter(self, text: str) -> None:
        """在桌宠头顶弹出一个临时闲聊气泡（独立窗口，4 秒后消失）。"""
        if self._mode != "resident" or not self.isVisible():
            return
        self._chatter = ChatterBubble(text, self)
        self._chatter.show()

    def _clamp_into_screen(self, pos: QPoint) -> QPoint:
        """把窗口位置限制在主屏可用区域内。"""
        screen = QApplication.primaryScreen()
        geo = (screen.availableGeometry() if screen is not None
               else QRect(0, 0, 1920, 1080))
        x = max(geo.left(), min(pos.x(), geo.right() - self.width()))
        y = max(geo.top(), min(pos.y(), geo.bottom() - self.height()))
        return QPoint(x, y)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        anchor_y = (self.height() - 4.0 - self._name_h
                    if self._pet_name else self.height() - 4.0)
        if self._pack.kind == "single":
            self._paint_single(painter, anchor_y)
            return
        pixmap = self._current_frame()
        if pixmap is None:
            return
        painter.drawPixmap(
            int((self.width() - pixmap.width()) / 2),
            int(anchor_y - pixmap.height()), pixmap)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer.stop()
        if not self._resolved and self._mode == "reminder":
            # 超时/离开撤销/Alt+F4：按“稍后再提醒”处理
            self._resolved = True
            self.snoozed.emit()
        self.closed.emit()
        event.accept()


class SedentaryPetWindow(PetWindow):
    """起身提醒桌宠：跳出挥手 + 自定义气泡文案；2 分钟未处理按推迟。"""

    def __init__(self, pack: CharacterPack,
                 message: str = "该站起来动动啦！", is_test: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(pack, mode="reminder", is_test=is_test,
                         bubble_text=message,
                         auto_close_ms=120_000, parent=parent)


class SitDownPetWindow(PetWindow):
    """坐下提示桌宠：乖巧坐着 + 自定义气泡文案；点击任意处或 15 秒后消失。"""

    def __init__(self, pack: CharacterPack, stand_seconds: int = 0,
                 message: str = "可以坐啦~", is_test: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        self._sit_text = message
        super().__init__(pack, mode="sit", is_test=is_test,
                         bubble_text=message,
                         auto_close_ms=15_000, parent=parent)
        if stand_seconds >= 60:
            duration = f"{stand_seconds // 60} 分 {stand_seconds % 60} 秒"
        else:
            duration = f"{stand_seconds} 秒"
        full_text = message + chr(10) + f"本次站立 {duration}，干得漂亮！"
        self._bubble._message.setText(full_text)


class ResidentPetWindow(PetWindow):
    """常驻待机桌宠：安静待在右下角，偶尔眨眼/呼吸；按住可拖拽位置。"""

    def __init__(self, pack: CharacterPack, name: str = "",
                 home: Optional[QPoint] = None,
                 parent: Optional[QWidget] = None) -> None:
        self._home = home
        super().__init__(pack, mode="resident", pet_name=name, parent=parent)

    def start(self) -> None:
        screen = QApplication.primaryScreen()
        geo = (screen.availableGeometry() if screen is not None
               else QRect(0, 0, 1920, 1080))
        if self._home is not None:
            self.move(self._clamp_into_screen(self._home))
            self.show()
            self.raise_()
            self._timer.start()
            return
        super().start()
        self._schedule_chatter()

    def _schedule_chatter(self) -> None:
        """安排下一次随机闲聊（5–10 分钟后，仅常驻桌宠）。"""
        QTimer.singleShot(random.randint(300, 600) * 1000,
                          self._idle_chatter)

    def _idle_chatter(self) -> None:
        if self.isVisible() and self._mode == "resident":
            self.show_chatter(random.choice(CHATTER_LINES))
        self._schedule_chatter()


class EyeBreakPetWindow(PetWindow):
    """护眼提醒桌宠：眨眼动画陪伴休息；桌宠眨眼模式下带台词气泡与倒计时。"""

    def __init__(self, pack: CharacterPack, message: str = "",
                 countdown_seconds: int = 0, postpone_minutes: int = 5,
                 is_test: bool = False, auto_close_ms: Optional[int] = None,
                 parent: Optional[QWidget] = None) -> None:
        self._countdown_total = max(0, int(countdown_seconds))
        self._message = message or "眺望远处，休息一下眼睛~"
        self._eye_snooze_label = f"{postpone_minutes} 分钟后提醒"
        super().__init__(pack, mode="break", is_test=is_test,
                         auto_close_ms=auto_close_ms,
                         bubble_text=self._message, parent=parent)

    def _on_tick(self) -> None:
        super()._on_tick()
        if self._bubble is not None and self._countdown_total > 0:
            remaining = max(0, self._countdown_total
                            - self._tick * self._interval_ms // 1000)
            self._bubble._message.setText(
                f"{self._message}\n{remaining} 秒后继续工作")


class ChatterBubble(QWidget):
    """桌宠头顶的临时闲聊气泡：独立透明顶层窗口，4 秒后自动消失。

    不挂在桌宠窗口内，避免常驻桌宠的鼠标拦截区域被永久放大。
    """

    def __init__(self, text: str, pet: "PetWindow") -> None:
        super().__init__(None)
        self._text = text
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(250, 56)
        # 位置：桌宠头顶居中，限制在屏幕内
        screen = QApplication.primaryScreen()
        geo = (screen.availableGeometry() if screen is not None
               else QRect(0, 0, 1920, 1080))
        x = pet.x() + pet.width() // 2 - self.width() // 2
        y = pet.y() - self.height() - 6
        x = max(geo.left(), min(x, geo.right() - self.width()))
        y = max(geo.top(), y)
        self.move(x, y)
        QTimer.singleShot(4000, self.deleteLater)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#8D6E63"), 2))
        painter.setBrush(QBrush(QColor(255, 255, 255, 246)))
        painter.drawRoundedRect(QRectF(1, 1, self.width() - 2,
                                       self.height() - 10), 14.0, 14.0)
        painter.setPen(QPen(QColor("#4E342E")))
        painter.drawText(QRectF(6, 4, self.width() - 12, self.height() - 18),
                         Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()
