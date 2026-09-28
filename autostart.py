"""开机自启管理：读写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run。"""
from __future__ import annotations

import sys
from pathlib import Path

from utils import LOG

try:
    import winreg
except ImportError:  # 仅在非 Windows 平台调试时会遇到
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "EyeGuard"


def _command() -> str:
    """生成自启命令行：打包后是 exe 本身；源码运行是 python main.py。"""
    if getattr(sys, "frozen", False):  # PyInstaller 环境
        return f'"{sys.executable}"'
    try:
        script = Path(sys.argv[0]).resolve()
    except (ValueError, OSError):
        script = Path("main.py")
    return f'"{sys.executable}" "{script}"'


def is_enabled() -> bool:
    """查询当前是否已写入自启（只读注册表，不修改）。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        LOG.error("读取自启注册表失败: %s", exc)
        return False


def enable() -> bool:
    """写入自启项，成功返回 True。"""
    if winreg is None:
        LOG.warning("当前平台不支持注册表自启")
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
    except OSError as exc:
        LOG.error("写入自启注册表失败: %s", exc)
        return False
    LOG.info("已开启开机自启: %s", _command())
    return True


def disable() -> bool:
    """删除自启项；项不存在时同样视为成功。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True
    except OSError as exc:
        LOG.error("删除自启注册表失败: %s", exc)
        return False
    LOG.info("已关闭开机自启")
    return True


def set_enabled(enabled: bool) -> bool:
    """统一开关入口。"""
    return enable() if enabled else disable()
