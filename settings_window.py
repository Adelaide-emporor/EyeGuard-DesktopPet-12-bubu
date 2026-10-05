"""设置窗口：全部可配置项 + 测试弹窗 + 保存/取消/恢复默认。"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog,
                               QComboBox, QDialog, QFileDialog,
                               QInputDialog, QProgressDialog,
                               QTabWidget,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSlider, QSpinBox, QTimeEdit,
                               QVBoxLayout, QWidget)

import autostart
from config import (MODE_FULLSCREEN, MODE_NOTIFICATION, MODE_PET, MODE_WINDOW,
                    AppConfig, save_config)
from utils import (APP_VERSION, LOG, SOUND_OPTIONS, build_app_icon,
                   play_sound,
                   sound_custom_dir, sound_player)


class SettingsDialog(QDialog):
    """EyeGuard 设置对话框。

    信号：
        saved(object)    点击“保存”且写入成功后发出，携带新 AppConfig
        testPopup(object) 点击“测试弹窗”时发出，携带表单当前值构造的 AppConfig
    """

    saved = Signal(object)
    testPopup = Signal(object)
    testSedentary = Signal(object)
    testSit = Signal(object)
    characterChanged = Signal(str)

    def __init__(self, cfg: AppConfig,
                 characters: Optional[List[Tuple[str, str]]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if hasattr(characters, "names"):  # CharacterManager 实例
            self._manager = characters
            self._character_items = characters.names()
        else:  # 兼容：[(文件夹名, 显示名), ...] 列表
            self._manager = None
            self._character_items = list(characters or [])
        from utils import APP_VERSION
        self.setWindowTitle(f"EyeGuard 设置 {APP_VERSION}")
        self.setWindowIcon(build_app_icon())
        self.setMinimumWidth(540)
        self._ready = False  # 装载表单期间不触发提示音试听
        self._build_ui()
        self._load(cfg)
        self._ready = True

    # ---------------- UI 构建 ----------------
    def _build_ui(self) -> None:
        self._work = QSpinBox(self)
        self._work.setRange(1, 240)
        self._work.setSuffix(" 分钟")

        self._break_sec = QSpinBox(self)
        self._break_sec.setRange(5, 600)
        self._break_sec.setSuffix(" 秒")

        self._postpone = QSpinBox(self)
        self._postpone.setRange(1, 120)
        self._postpone.setSuffix(" 分钟")

        self._title = QLineEdit(self)
        self._message = QPlainTextEdit(self)
        self._message.setFixedHeight(72)

        self._mode = QComboBox(self)
        self._mode.addItem("全屏置顶遮罩", MODE_FULLSCREEN)
        self._mode.addItem("普通窗口", MODE_WINDOW)
        self._mode.addItem("仅系统通知（托盘气泡）", MODE_NOTIFICATION)
        self._mode.addItem("桌宠眨眼（无弹窗）", MODE_PET)

        self._on_top = QCheckBox("普通窗口置顶显示", self)
        self._sound = QCheckBox("提醒时播放提示音", self)
        self._volume = QSlider(Qt.Orientation.Horizontal, self)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume_label = QLabel("80%", self)
        self._volume_label.setMinimumWidth(40)
        self._volume.valueChanged.connect(self._on_volume_changed)
        volume_row = QHBoxLayout()
        volume_row.addWidget(self._volume)
        volume_row.addWidget(self._volume_label)
        self._eye_char = QComboBox(self)
        for folder, name in self._character_items:
            self._eye_char.addItem(name, folder)
        self._eye_sound = QComboBox(self)
        for key, label in SOUND_OPTIONS:
            self._eye_sound.addItem(label, key)
        self._eye_sound.currentIndexChanged.connect(
            lambda _i: self._preview_sound(self._eye_sound))
        btn_import_eye = QPushButton("导入...", self)
        btn_import_eye.setToolTip("从本地选择 wav/mp3/m4a 作为护眼提醒音")
        btn_import_eye.clicked.connect(
            lambda: self._import_sound(self._eye_sound))
        eye_row = QHBoxLayout()
        eye_row.addWidget(self._eye_sound, 1)
        eye_row.addWidget(btn_import_eye)

        self._dnd_enabled = QCheckBox("启用免打扰时段", self)
        self._dnd_start = QTimeEdit(self)
        self._dnd_start.setDisplayFormat("HH:mm")
        self._dnd_end = QTimeEdit(self)
        self._dnd_end.setDisplayFormat("HH:mm")

        dnd_row = QHBoxLayout()
        dnd_row.addWidget(self._dnd_start)
        dnd_row.addWidget(QLabel("至", self))
        dnd_row.addWidget(self._dnd_end)
        dnd_row.addStretch(1)
        dnd_box = QGroupBox("免打扰（时段内到点不弹提醒，静默续期）", self)
        dnd_layout = QVBoxLayout(dnd_box)
        dnd_layout.addWidget(self._dnd_enabled)
        dnd_layout.addLayout(dnd_row)
        self._dnd_enabled.toggled.connect(self._sync_dnd)

        # ---- 久坐提醒 ----
        self._sed_enabled = QCheckBox(
            "启用久坐提醒（独立于护眼提醒计时）", self)
        self._sed_interval = QSpinBox(self)
        self._sed_interval.setRange(20, 60)
        self._sed_interval.setSuffix(" 分钟")
        self._sed_stand = QSpinBox(self)
        self._sed_stand.setRange(3, 10)
        self._sed_stand.setSuffix(" 分钟")
        self._sed_cap = QSpinBox(self)
        self._sed_cap.setRange(0, 600)
        self._sed_cap.setSuffix(" 分钟")
        self._sed_cap.setSpecialValueText("不提醒（0）")
        self._stand_sound = QComboBox(self)
        for key, label in SOUND_OPTIONS:
            self._stand_sound.addItem(label, key)
        self._stand_sound.currentIndexChanged.connect(
            lambda _i: self._preview_sound(self._stand_sound))
        btn_import_stand = QPushButton("导入...", self)
        btn_import_stand.setToolTip("从本地选择 wav/mp3/m4a 作为站立提醒音")
        btn_import_stand.clicked.connect(
            lambda: self._import_sound(self._stand_sound))
        stand_row = QHBoxLayout()
        stand_row.addWidget(self._stand_sound, 1)
        stand_row.addWidget(btn_import_stand)

        self._sit_sound = QComboBox(self)
        for key, label in SOUND_OPTIONS:
            self._sit_sound.addItem(label, key)
        self._sit_sound.currentIndexChanged.connect(
            lambda _i: self._preview_sound(self._sit_sound))
        btn_import_sit = QPushButton("导入...", self)
        btn_import_sit.setToolTip("从本地选择 wav/mp3/m4a 作为坐下提醒音")
        btn_import_sit.clicked.connect(
            lambda: self._import_sound(self._sit_sound))
        sit_row = QHBoxLayout()
        sit_row.addWidget(self._sit_sound, 1)
        sit_row.addWidget(btn_import_sit)

        # 站立/坐下各自独立选择桌宠角色
        self._stand_char = QComboBox(self)
        self._sit_char = QComboBox(self)
        for folder, name in self._character_items:
            self._stand_char.addItem(name, folder)
            self._sit_char.addItem(name, folder)

        self._resident = QCheckBox(
            "桌宠常驻右下角待机（眨眼/呼吸动画，点击穿透）", self)

        sed_form = QFormLayout()
        sed_form.addRow("久坐提醒间隔", self._sed_interval)
        sed_form.addRow("站立活动时长", self._sed_stand)
        sed_form.addRow("每日站立上限", self._sed_cap)
        sed_form.addRow("站立提醒音", stand_row)
        sed_form.addRow("坐下提醒音", sit_row)
        self._stand_add = QPushButton("添加...", self)
        self._stand_add.setToolTip("上传图片自动抠图，作为站立提醒角色")
        self._stand_add.clicked.connect(
            lambda: self._add_custom_character(self._stand_char))
        stand_char_row = QHBoxLayout()
        stand_char_row.addWidget(self._stand_char, 1)
        stand_char_row.addWidget(self._stand_add)
        self._sit_add = QPushButton("添加...", self)
        self._sit_add.setToolTip("上传图片自动抠图，作为坐下提醒角色")
        self._sit_add.clicked.connect(
            lambda: self._add_custom_character(self._sit_char))
        sit_char_row = QHBoxLayout()
        sit_char_row.addWidget(self._sit_char, 1)
        sit_char_row.addWidget(self._sit_add)
        sed_form.addRow("站立提醒角色", stand_char_row)
        sed_form.addRow("坐下提醒角色", sit_char_row)
        self._stand_message = QLineEdit(self)
        self._stand_message.setMaxLength(40)
        self._stand_message.setPlaceholderText("该站起来动动啦！")
        sed_form.addRow("站立提醒文案", self._stand_message)
        self._sit_message = QLineEdit(self)
        self._sit_message.setMaxLength(40)
        self._sit_message.setPlaceholderText("可以坐啦~")
        sed_form.addRow("坐下提醒文案", self._sit_message)
        sed_box = QGroupBox("久坐提醒（建议每 30 分钟起身活动 3–10 分钟）", self)
        sed_layout = QVBoxLayout(sed_box)
        sed_layout.addWidget(self._sed_enabled)
        sed_layout.addLayout(sed_form)
        sed_layout.addWidget(self._resident)
        self._sed_enabled.toggled.connect(self._sync_sedentary)

        # ---- 桌宠名字与形象 ----
        self._pet_name = QLineEdit(self)
        self._pet_name.setMaxLength(12)
        self._pet_name.setPlaceholderText("夏cc")
        self._char_combo = QComboBox(self)
        for folder, name in self._character_items:
            self._char_combo.addItem(name, folder)
        self._char_combo.currentIndexChanged.connect(
            self._on_character_changed)

        self._autostart = QCheckBox(
            "开机自动启动（写入注册表 HKCU\\...\\Run）", self)

        # ---- 选项卡布局：护眼提醒 / 久坐提醒 / 桌宠与通用 ----
        self._tabs = QTabWidget(self)

        eye_tab = QWidget()
        eye_form = QFormLayout(eye_tab)
        self._eye_enabled = QCheckBox("启用护眼提醒", self)
        eye_form.addRow("", self._eye_enabled)
        eye_form.addRow("工作时长", self._work)
        eye_form.addRow("休息时长", self._break_sec)
        eye_form.addRow("推迟时长", self._postpone)
        self._away_reset = QSpinBox(self)
        self._away_reset.setRange(60, 1800)
        self._away_reset.setSuffix(" 秒")
        self._away_reset.setToolTip(
            "键鼠空闲超过该时长才视为离开休息过并重置护眼周期；"
            "短时间离开回来后继续原倒计时；"
            "锁屏/屏保/关屏/睡眠始终立即重置，不受此值影响。")
        eye_form.addRow("离开视为休息", self._away_reset)
        eye_form.addRow("弹窗标题", self._title)
        eye_form.addRow("弹窗正文", self._message)
        eye_form.addRow("提醒方式", self._mode)
        self._mask_theme = QComboBox(self)
        from mask_theme import THEMES
        for key, (display, _hex) in THEMES.items():
            self._mask_theme.addItem(display, key)
        self._mask_custom = ""
        self._mask_color_btn = QPushButton("选色...", self)
        self._mask_color_btn.setToolTip(
            "打开调色盘：在色域中点选颜色，用滑条调节色相与深浅，"
            "也可直接输入 RGB/HSV 数值或吸取屏幕颜色")
        self._mask_color_btn.clicked.connect(self._pick_mask_color)
        self._mask_chip = QLabel(self)
        self._mask_chip.setMinimumWidth(96)
        self._mask_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mask_row = QHBoxLayout()
        mask_row.addWidget(self._mask_theme, 1)
        mask_row.addWidget(self._mask_color_btn)
        mask_row.addWidget(self._mask_chip)
        self._mask_theme.currentIndexChanged.connect(
            self._on_mask_theme_changed)
        eye_form.addRow("遮罩主题", mask_row)
        eye_form.addRow("", self._on_top)
        eye_form.addRow("", self._sound)
        eye_form.addRow("提示音音量", volume_row)
        self._eye_add = QPushButton("添加...", self)
        self._eye_add.setToolTip("上传图片自动抠图，作为护眼提醒角色")
        self._eye_add.clicked.connect(
            lambda: self._add_custom_character(self._eye_char))
        eye_char_row = QHBoxLayout()
        eye_char_row.addWidget(self._eye_char, 1)
        eye_char_row.addWidget(self._eye_add)
        eye_form.addRow("护眼提醒角色", eye_char_row)
        eye_form.addRow("护眼提醒音", eye_row)
        eye_form.addRow("", dnd_box)

        sed_tab = QWidget()
        sed_layout = QVBoxLayout(sed_tab)
        sed_layout.addWidget(sed_box)
        sed_layout.addStretch(1)

        general_tab = QWidget()
        general_form = QFormLayout(general_tab)
        general_form.addRow("桌宠名字", self._pet_name)
        self._gen_add = QPushButton("添加...", self)
        self._gen_add.setToolTip("上传图片自动抠图，作为常驻待机桌宠")
        self._gen_add.clicked.connect(
            lambda: self._add_custom_character(self._char_combo))
        gen_char_row = QHBoxLayout()
        gen_char_row.addWidget(self._char_combo, 1)
        gen_char_row.addWidget(self._gen_add)
        general_form.addRow("桌宠形象", gen_char_row)
        general_form.addRow("", self._autostart)
        self._autostart_delay = QSpinBox(self)
        self._autostart_delay.setRange(0, 300)
        self._autostart_delay.setSuffix(" 秒")
        self._autostart_delay.setSpecialValueText("立即启动")
        self._autostart_delay.setToolTip(
            "开机登录后延迟多久启动 EyeGuard（仅在勾选开机自启时生效）")
        general_form.addRow("自启延迟", self._autostart_delay)
        hotkey_hint = QLabel(
            "全局快捷键：Ctrl+Alt+B 立即休息 · Ctrl+Alt+P 暂停/恢复", self)
        hotkey_hint.setStyleSheet("color:#90A4AE; font-size:12px;")
        general_form.addRow("", hotkey_hint)

        self._tabs.addTab(eye_tab, "护眼提醒")
        self._tabs.addTab(sed_tab, "久坐提醒")
        self._tabs.addTab(general_tab, "桌宠与通用")

        btn_test = QPushButton("测试护眼提醒", self)
        btn_test_sed = QPushButton("测试站立提醒", self)
        btn_test_sit = QPushButton("测试坐下提醒", self)
        btn_reset = QPushButton("恢复默认", self)
        btn_save = QPushButton("保存", self)
        btn_cancel = QPushButton("取消", self)
        btn_save.setDefault(True)
        btn_test.clicked.connect(self._on_test)
        btn_test_sed.clicked.connect(self._on_test_sedentary)
        btn_test_sit.clicked.connect(self._on_test_sit)
        btn_reset.clicked.connect(self._on_reset)
        btn_save.clicked.connect(self._on_save)
        btn_cancel.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(btn_test)
        btn_row.addWidget(btn_test_sed)
        btn_row.addWidget(btn_test_sit)
        btn_row.addWidget(btn_reset)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_cancel)

        root = QVBoxLayout(self)
        root.addWidget(self._tabs)   # 选项卡区域
        root.addLayout(btn_row)      # 底部按钮区固定，不随选项卡滚动

    # ---------------- 装载 / 收集 ----------------
    def _load(self, cfg: AppConfig) -> None:
        """把配置对象填充到表单（自启项以注册表实际状态为准）。"""
        self._work.setValue(cfg.work_minutes)
        self._break_sec.setValue(cfg.break_seconds)
        self._postpone.setValue(cfg.postpone_minutes)
        self._title.setText(cfg.popup_title)
        self._message.setPlainText(cfg.popup_message)
        index = self._mode.findData(cfg.popup_mode)
        self._mode.setCurrentIndex(max(0, index))
        self._on_top.setChecked(cfg.always_on_top)
        self._sound.setChecked(cfg.sound_enabled)
        self._dnd_enabled.setChecked(cfg.dnd_enabled)
        start = QTime.fromString(cfg.dnd_start, "HH:mm")
        end = QTime.fromString(cfg.dnd_end, "HH:mm")
        self._dnd_start.setTime(start if start.isValid() else QTime(22, 0))
        self._dnd_end.setTime(end if end.isValid() else QTime(7, 0))
        self._autostart.setChecked(autostart.is_enabled())
        self._volume.setValue(cfg.volume)
        self._volume_label.setText(f"{cfg.volume}%")
        self._ensure_item(self._eye_char, cfg.eye_character)
        self._ensure_sound_item(self._eye_sound, cfg.eye_sound)
        self._sed_enabled.setChecked(cfg.sedentary_enabled)
        self._sed_interval.setValue(cfg.sedentary_interval_minutes)
        self._sed_stand.setValue(cfg.stand_minutes)
        self._sed_cap.setValue(cfg.daily_stand_cap_minutes)
        self._ensure_item(self._stand_char, cfg.stand_character)
        self._ensure_item(self._sit_char, cfg.sit_character)
        self._ensure_sound_item(self._stand_sound, cfg.stand_sound)
        self._ensure_sound_item(self._sit_sound, cfg.sit_sound)
        self._resident.setChecked(cfg.resident_pet)
        self._stand_message.setText(cfg.stand_message)
        self._sit_message.setText(cfg.sit_message)
        self._autostart_delay.setValue(cfg.autostart_delay_seconds)
        self._away_reset.setValue(cfg.away_reset_seconds)
        self._eye_enabled.setChecked(cfg.eye_care_enabled)
        mask_index = self._mask_theme.findData(cfg.mask_theme)
        self._mask_theme.setCurrentIndex(max(0, mask_index))
        self._mask_custom = cfg.mask_custom_color
        self._sync_mask_theme()
        self._pet_name.setText(cfg.pet_name)
        char_index = self._char_combo.findData(cfg.pet_character)
        self._char_combo.setCurrentIndex(max(0, char_index))
        self._sync_dnd()
        self._sync_sedentary()

    def _collect(self) -> AppConfig:
        """从表单收集一个经过校验的 AppConfig。"""
        cfg = AppConfig()
        cfg.work_minutes = self._work.value()
        cfg.break_seconds = self._break_sec.value()
        cfg.postpone_minutes = self._postpone.value()
        cfg.popup_title = self._title.text()
        cfg.popup_message = self._message.toPlainText()
        cfg.popup_mode = self._mode.currentData() or MODE_FULLSCREEN
        cfg.always_on_top = self._on_top.isChecked()
        cfg.sound_enabled = self._sound.isChecked()
        cfg.dnd_enabled = self._dnd_enabled.isChecked()
        cfg.dnd_start = self._dnd_start.time().toString("HH:mm")
        cfg.dnd_end = self._dnd_end.time().toString("HH:mm")
        cfg.autostart = self._autostart.isChecked()
        cfg.volume = self._volume.value()
        cfg.sedentary_enabled = self._sed_enabled.isChecked()
        cfg.sedentary_interval_minutes = self._sed_interval.value()
        cfg.stand_minutes = self._sed_stand.value()
        cfg.daily_stand_cap_minutes = self._sed_cap.value()
        cfg.stand_character = self._stand_char.currentData() or "yier"
        cfg.sit_character = self._sit_char.currentData() or "yier"
        cfg.stand_message = self._stand_message.text().strip() or "该站起来动动啦！"
        cfg.sit_message = self._sit_message.text().strip() or "可以坐啦~"
        cfg.autostart_delay_seconds = self._autostart_delay.value()
        cfg.away_reset_seconds = self._away_reset.value()
        cfg.stand_sound = self._stand_sound.currentData() or "pop"
        cfg.sit_sound = self._sit_sound.currentData() or "sparkle"
        cfg.eye_care_enabled = self._eye_enabled.isChecked()
        cfg.mask_theme = self._mask_theme.currentData() or "teal"
        cfg.mask_custom_color = self._mask_custom
        cfg.eye_character = self._eye_char.currentData() or "yier"
        cfg.eye_sound = self._eye_sound.currentData() or "ding"
        cfg.pet_name = self._pet_name.text().strip() or "夏cc"
        if self._char_combo.currentData():
            cfg.pet_character = self._char_combo.currentData()
        cfg.resident_pet = self._resident.isChecked()
        return cfg.sanitized()

    def _on_mask_theme_changed(self, index: int) -> None:
        """用户选择预设主题：退出自定义色，让预设立即生效。"""
        self._mask_custom = ""
        self._sync_mask_theme()

    def _sync_mask_theme(self) -> None:
        """刷新“当前颜色”色块：自定义色优先，否则显示下拉预设的色。"""
        from mask_theme import THEMES
        preset_hex = THEMES.get(
            self._mask_theme.currentData() or "teal", ("", "#26A69A"))[1]
        effective = self._mask_custom or preset_hex
        self._mask_chip.setText(effective)
        self._mask_chip.setStyleSheet(
            f"background-color:{effective}; color:#FFFFFF;"
            "border:1px solid #90A4AE; border-radius:4px; font-weight:600;")
        if self._mask_custom:
            self._mask_color_btn.setText("重选颜色...")
        else:
            self._mask_color_btn.setText("选色...")

    def _pick_mask_color(self) -> None:
        """打开 Photoshop 风格调色盘（色域点选 + 色相/深浅滑条 + RGB/HSV）。"""
        from PySide6.QtGui import QColor
        LOG.info("用户点击选色：打开调色盘")
        initial = QColor(self._mask_custom if self._mask_custom
                         else "#26A69A")
        color = QColorDialog.getColor(
            initial, self, "选择遮罩主题色",
            QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if color.isValid():
            LOG.info("选色完成: %s", color.name())
            self._mask_custom = color.name()
            self._sync_mask_theme()
        else:
            LOG.info("选色已取消")

    def _sync_dnd(self) -> None:
        enabled = self._dnd_enabled.isChecked()
        self._dnd_start.setEnabled(enabled)
        self._dnd_end.setEnabled(enabled)

    def _sync_sedentary(self) -> None:
        enabled = self._sed_enabled.isChecked()
        self._sed_interval.setEnabled(enabled)
        self._sed_stand.setEnabled(enabled)
        self._sed_cap.setEnabled(enabled)
        self._stand_sound.setEnabled(enabled)
        self._sit_sound.setEnabled(enabled)
        self._stand_char.setEnabled(enabled)
        self._sit_char.setEnabled(enabled)
        self._resident.setEnabled(enabled)

    def _ensure_item(self, combo: QComboBox, folder: str) -> None:
        """确保角色组合框里存在指定包（配置里残留已删除包时补一项）。"""
        index = combo.findData(folder)
        if index < 0 and folder:
            combo.addItem(folder, folder)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    def _preview_sound(self, combo: QComboBox) -> None:
        """下拉选择即试听（装载表单期间不触发）。"""
        if self._ready:
            play_sound(combo.currentData())

    def _on_volume_changed(self, value: int) -> None:
        """音量滑块实时应用到播放器（默认 80，永不为 0；0 才是真正静音）。"""
        self._volume_label.setText(f"{value}%")
        sound_player().set_volume(value)

    def _ensure_sound_item(self, combo: QComboBox, key: str) -> None:
        """确保组合框里存在 key（自定义 file: 项按需补充）并选中。"""
        index = combo.findData(key)
        if index < 0 and key and key.startswith("file:"):
            combo.addItem(f"自定义：{Path(key[5:]).name}", key)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))

    def _import_sound(self, combo: QComboBox) -> None:
        """导入本地 wav/mp3/m4a 作为提醒音：复制到自定义目录并选中试听。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择提示音文件", "",
            "音频文件 (*.wav *.mp3 *.m4a *.m3a *.aac)")
        if not path:
            return
        dest = sound_custom_dir() / (
            f"{datetime.now():%Y%m%d_%H%M%S}_{Path(path).name}")
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            QMessageBox.warning(self, "EyeGuard", f"导入失败：{exc}")
            return
        key = "file:" + str(dest)
        existing = combo.findData(key)
        if existing >= 0:
            combo.setCurrentIndex(existing)
            return
        combo.addItem(f"自定义：{dest.name}", key)
        combo.setCurrentIndex(combo.count() - 1)

    def _refresh_character_items(self) -> None:
        names = (self._manager.names()
                 if hasattr(self._manager, 'names') else self._character_items)
        for combo in (self._eye_char, self._stand_char, self._sit_char,
                      self._char_combo):
            selected = combo.currentData()
            combo.clear()
            for folder, name in names:
                combo.addItem(name, folder)
            idx = combo.findData(selected)
            combo.setCurrentIndex(max(0, idx))

    def _custom_packs_root(self):
        from utils import app_data_dir
        return app_data_dir() / 'characters'

    def _add_custom_character(self, combo: Optional[QComboBox] = None) -> None:
        """上传图片 → 自动抠图 → 生成角色包 → 选入下拉框 → 弹出预览。"""
        import threading
        import time as _time
        from PySide6.QtWidgets import QProgressDialog
        from matting import MattingError, create_custom_pack
        LOG.info("用户开始添加自定义角色")
        files, _f = QFileDialog.getOpenFileNames(
            self, '选择角色图片（可多选，按文件名关键字映射动作）', '',
            '图片 (*.png *.jpg *.jpeg *.webp)')
        if not files:
            return
        name, ok = QInputDialog.getText(
            self, '添加自定义角色', '给角色起个名字（显示在桌宠下方）：',
            text='我的角色')
        if not ok:
            return

        # 后台线程抠图，主界面保持响应；带进度提示，避免“点了没反应”
        result: dict = {}
        progress = QProgressDialog(
            '正在抠图并生成角色包，请稍候（几秒钟）…', None, 0, 0, self)
        progress.setWindowTitle('添加自定义角色')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        def _worker() -> None:
            try:
                result['info'] = create_custom_pack(
                    [Path(f) for f in files], name,
                    self._custom_packs_root())
            except Exception as exc:  # 抠图/写盘失败统一兜底
                result['error'] = exc
            result['done'] = True

        threading.Thread(target=_worker, daemon=True,
                         name='EyeGuard-Matting').start()
        while not result.get('done'):
            QApplication.processEvents()
            _time.sleep(0.05)
        progress.close()

        if 'error' in result:
            exc = result['error']
            if isinstance(exc, MattingError):
                QMessageBox.warning(
                    self, '抠图失败',
                    str(exc) + chr(10)
                    + '请上传背景更简单的图片，或手动抠图后再上传。')
            else:
                LOG.warning("添加自定义角色失败: %s", exc)
                QMessageBox.warning(self, '添加失败', str(exc))
            return

        info = result['info']
        if hasattr(self._manager, 'rescan'):
            self._manager.rescan()
        self._character_items = self._manager.names()
        self._refresh_character_items()
        folder = info['dir'].name
        if combo is not None:  # 在发起添加的下拉框中选中新角色
            idx = combo.findData(folder)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        # 完成预览：直接展示抠图效果
        preview = QPixmap(str(Path(info['dir']) / 'character.png'))
        box = QMessageBox(self)
        box.setWindowTitle('添加成功')
        box.setText(f"角色「{info['name']}」已生成并选中。\n"
                    "可用对应选项卡的测试按钮查看实际效果。")
        box.setIconPixmap(preview.scaled(
            160, 160, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        box.exec()

    def _custom_packs_root(self):
        from utils import app_data_dir
        return app_data_dir() / 'characters'

    def _add_custom_character(self, combo: Optional[QComboBox] = None) -> None:
        from matting import MattingError, create_custom_pack
        files, _f = QFileDialog.getOpenFileNames(
            self, '选择角色图片（可多选，按文件名关键字映射动作）', '',
            '图片 (*.png *.jpg *.jpeg *.webp)')
        if not files:
            return
        name, ok = QInputDialog.getText(
            self, '添加自定义角色', '给角色起个名字：', text='我的角色')
        if not ok:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            info = create_custom_pack(
                [Path(f) for f in files], name, self._custom_packs_root())
        except MattingError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, '抠图失败', str(
                exc) + chr(10) + '请上传背景更简单的图片，或手动抠图后再上传。')
            return
        except (OSError, ValueError) as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, '添加失败', str(exc))
            return
        QApplication.restoreOverrideCursor()
        if hasattr(self._manager, 'rescan'):
            self._manager.rescan()
        self._character_items = self._manager.names()
        self._refresh_character_items()
        folder = info["dir"].name
        if combo is not None:  # 在发起添加的下拉框中选中新角色
            idx = combo.findData(folder)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        QMessageBox.information(
            self, '添加自定义角色',
            '角色「' + info['name'] + '」已生成并出现在列表中。'
            '请确保上传的图片拥有合法使用权，仅供个人使用。')

    def _on_character_changed(self, index: int) -> None:
        """切换桌宠形象：立即生效并由主窗口自动保存。"""
        if self._ready and 0 <= index < self._char_combo.count():
            folder = self._char_combo.itemData(index)
            if folder:
                self.characterChanged.emit(str(folder))

    # ---------------- 动作 ----------------
    def _on_test(self) -> None:
        """用当前表单值弹一次测试窗口（不保存、不计入统计、不影响计时）。"""
        self.testPopup.emit(self._collect())

    def _on_test_sedentary(self) -> None:
        """用当前表单值弹一次站立提醒测试（桌宠 + 音效）。"""
        self.testSedentary.emit(self._collect())

    def _on_test_sit(self) -> None:
        """用当前表单值弹一次坐下提醒测试（桌宠 + 音效）。"""
        self.testSit.emit(self._collect())

    def _on_reset(self) -> None:
        """把表单恢复为出厂默认值（未保存前不生效）。"""
        self._load(AppConfig())

    def _on_save(self) -> None:
        """保存配置 + 同步注册表自启，然后发出 saved 并关闭。"""
        cfg = self._collect()
        if not save_config(cfg):
            QMessageBox.warning(
                self, "EyeGuard", "配置保存失败，请检查磁盘权限或查看日志。")
            return
        if not autostart.set_enabled(cfg.autostart,
                                     cfg.autostart_delay_seconds):
            QMessageBox.warning(
                self, "EyeGuard", "开机自启写入注册表失败，请查看日志。")
        self.saved.emit(cfg)
        self.accept()
