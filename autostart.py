"""开机自启管理：读写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run。

支持自启延迟：delay>0 时生成一个隐藏的 wscript 脚本（先 Sleep 再启动程序），
Run 键值指向 wscript.exe —— 用户登录后由系统拉起脚本，静默等待后启动 EyeGuard，
避免与登录时的其他程序抢占资源。无延迟时 Run 键值直接指向程序本身。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Tuple

from utils import LOG, app_data_dir

try:
    import winreg
except ImportError:  # 仅在非 Windows 平台调试时会遇到
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "EyeGuard"


def _vbs_path() -> Path:
    """延迟启动脚本位置：%APPDATA%\\EyeGuard\\autostart.vbs。"""
    return app_data_dir() / "autostart.vbs"


def _exe_command() -> str:
    """直接启动命令：打包后是 exe 本身；源码运行是 python main.py。"""
    if getattr(sys, "frozen", False):  # PyInstaller 环境
        return f'"{sys.executable}"'
    try:
        script = Path(sys.argv[0]).resolve()
    except (ValueError, OSError):
        script = Path("main.py")
    return f'"{sys.executable}" "{script}"'


def autostart_command(delay_seconds: int = 0) -> Tuple[str, Optional[Path]]:
    """计算自启命令：返回 (Run 键值, 需要写入的 vbs 路径或 None)。

    delay>0 时生成 vbs（WScript.Sleep 后再启动程序，无窗口），键值指向
    wscript.exe；delay=0 时键值直接指向程序本身。此函数不写注册表。
    """
    if delay_seconds <= 0:
        return _exe_command(), None
    vbs = _vbs_path()
    script = (f"WScript.Sleep {int(delay_seconds) * 1000}\n"
              f'CreateObject("WScript.Shell").Run """{_exe_command()}""", 1, False\n')
    return f'wscript.exe "{vbs}"', vbs


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


def enable(delay_seconds: int = 0) -> bool:
    """写入自启项（含可选延迟），成功返回 True。"""
    if winreg is None:
        LOG.warning("当前平台不支持注册表自启")
        return False
    value, vbs_path = autostart_command(delay_seconds)
    if vbs_path is not None:
        try:
            script = (f"WScript.Sleep {int(delay_seconds) * 1000}\n"
                      f'CreateObject("WScript.Shell").Run '
                      f'"""{_exe_command()}""", 1, False\n')
            vbs_path.write_text(script, encoding="utf-8")
        except OSError as exc:
            LOG.error("写入自启延迟脚本失败: %s", exc)
            return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)
    except OSError as exc:
        LOG.error("写入自启注册表失败: %s", exc)
        return False
    if delay_seconds > 0:
        LOG.info("已开启开机自启（延迟 %d 秒，经 wscript）: %s",
                 delay_seconds, value)
    else:
        LOG.info("已开启开机自启: %s", value)
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


def set_enabled(enabled: bool, delay_seconds: int = 0) -> bool:
    """统一开关入口（延迟仅在有注册表自启的平台生效）。"""
    if winreg is None:
        return False
    return enable(delay_seconds) if enabled else disable()
