"""手动框选眨眼区域：在角色图上拖出最多两个矩形（左右眼）。

保存后区域以归一化坐标写入角色包 manifest（eye_rects），
眨眼动画会用区域上方取样的肤色短暂覆盖眼睛（见 pet_window/_paint_single）。
"""
from __future__ import annotations

from typing import List

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QPushButton

from utils import build_app_icon

_MAX_RECTS = 2
_DRAW_W = 460


class EyeRectPickerDialog(QDialog):
    """拖出最多两个矩形框选左右眼；“清除”重画，“保存”写入角色包。"""

    def __init__(self, image_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("框选眨眼区域")
        self.setWindowIcon(build_app_icon())
        self._source = QPixmap(str(image_path))
        self._scale = min(1.0, _DRAW_W / max(1, self._source.width()))
        self._draw_w = int(self._source.width() * self._scale)
        self._draw_h = int(self._source.height() * self._scale)
        self._offset_x = (_DRAW_W + 40 - self._draw_w) // 2
        self._offset_y = 16
        self._rects: List[QRectF] = []       # 显示坐标
        self._draw_start: Optional[QPoint] = None
        self._cursor_rect: Optional[QRectF] = None

        self.setFixedSize(_DRAW_W + 40, self._draw_h + 130)

        hint = QLabel("用鼠标在眼睛位置拖出矩形（最多两个，先左后右）；"
                      "眨眼时将以肤色短暂覆盖该区域。", self)
        hint.setStyleSheet("color:#666; font-size:12px;")
        hint.setGeometry(12, 4, self.width() - 24, 24)

        btn_clear = QPushButton("清除重选", self)
        btn_clear.setGeometry(16, self.height() - 40, 96, 28)
        btn_clear.clicked.connect(self._clear)
        btn_save = QPushButton("保存", self)
        btn_save.setGeometry(self.width() // 2 - 100, self.height() - 40,
                             92, 28)
        btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("取消", self)
        btn_cancel.setGeometry(self.width() // 2 + 8, self.height() - 40,
                               92, 28)
        btn_cancel.clicked.connect(self.reject)

    def rects_norm(self) -> List[Tuple[float, float, float, float]]:
        """归一化 (x, y, w, h) 列表。"""
        w = max(1, self._draw_w)
        h = max(1, self._draw_h)
        return [(r.x() / w, r.y() / h, r.width() / w, r.height() / h)
                for r in self._rects]

    def _clear(self) -> None:
        self._rects.clear()
        self._cursor_rect = None
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if self._point_in_image(pos):
                self._draw_start = pos
                self._cursor_rect = QRectF(pos, pos)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._draw_start is not None:
            pos = event.position().toPoint()
            self._cursor_rect = QRectF(
                min(self._draw_start.x(), pos.x()),
                min(self._draw_start.y(), pos.y()),
                abs(pos.x() - self._draw_start.x()),
                abs(pos.y() - self._draw_start.y()))
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._draw_start is not None:
            rect = self._cursor_rect
            self._draw_start = None
            self._cursor_rect = None
            if rect is not None and rect.width() > 4 and rect.height() > 4:
                if len(self._rects) >= _MAX_RECTS:
                    self._rects.pop(0)
                self._rects.append(QRectF(rect))
            self.update()
        event.accept()

    def _point_in_image(self, pos) -> bool:
        return (self._offset_x <= pos.x() <= self._offset_x + self._draw_w
                and self._offset_y <= pos.y()
                <= self._offset_y + self._draw_h)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawPixmap(self._offset_x, self._offset_y, self._source)
        pen = QPen(QColor("#2E7D32"), 2)
        painter.setPen(pen)
        for rect in self._rects:
            painter.drawRect(rect)
        if self._cursor_rect is not None:
            painter.setPen(QPen(QColor("#1565C0"), 2))
            painter.drawRect(self._cursor_rect)
        painter.end()


class _DragPreview(QWidget):
    """内部占位（未使用，保留以避免误导入）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
