"""EyeGuard 通用工具：数据目录、日志、单实例守卫、免打扰判断、提示音、内置图标。"""
from __future__ import annotations

import array
import ctypes
import io
import logging
import logging.handlers
import math
import os
import struct
import subprocess
import sys
import threading
import time
import wave as wave_module
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket

try:
    import winsound
except ImportError:  # 非 Windows 平台兜底
    winsound = None  # type: ignore[assignment]

APP_NAME = "EyeGuard"
APP_VERSION = "v1.5.0"
LOG = logging.getLogger(APP_NAME)


def app_data_dir() -> Path:
    """返回数据目录 %APPDATA%\\EyeGuard（不存在则自动创建）。"""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOG.error("创建数据目录失败: %s", exc)
    return path


def config_path() -> Path:
    """配置文件完整路径。"""
    return app_data_dir() / "config.json"


def stats_path() -> Path:
    """统计文件完整路径。"""
    return app_data_dir() / "stats.json"


def log_dir() -> Path:
    """日志目录（不存在则创建）。"""
    path = app_data_dir() / "logs"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def setup_logging(debug: bool = False) -> None:
    """初始化滚动文件日志；debug=True 时额外输出到控制台。"""
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(module)s.%(funcName)s: %(message)s")
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_dir() / "eyeguard.log",
            maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError as exc:
        # 日志初始化失败不应阻断程序，仅打印到 stderr
        print(f"日志文件创建失败: {exc}", file=sys.stderr)
    if debug:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)


class SingleInstance:
    """基于 QLocalServer 的单实例守卫（Windows 上为命名管道）。"""

    def __init__(self, key: str) -> None:
        self._key = key
        self._server: Optional[QLocalServer] = None

    def try_lock(self) -> bool:
        """尝试占用唯一实例锁；已有实例在运行时返回 False。"""
        probe = QLocalSocket()
        probe.connectToServer(self._key)
        if probe.waitForConnected(300):
            probe.disconnectFromServer()
            return False  # 已有实例在运行
        # 清理上次异常退出残留的服务名后重新监听
        QLocalServer.removeServer(self._key)
        server = QLocalServer()
        if not server.listen(self._key):
            LOG.error("单实例服务监听失败: %s", server.errorString())
        self._server = server
        return True

    def release(self) -> None:
        """释放实例锁（程序退出时调用）。"""
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._key)
            self._server = None


def _parse_hhmm(text: str) -> Optional[dtime]:
    """解析 'HH:MM' 字符串为 time 对象；非法输入返回 None。"""
    try:
        hour, minute = text.strip().split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def in_dnd(start: str, end: str, now: Optional[datetime] = None) -> bool:
    """判断 now 是否处于免打扰区间，支持跨午夜（如 22:00-07:00）。"""
    begin = _parse_hhmm(start)
    finish = _parse_hhmm(end)
    if begin is None or finish is None or begin == finish:
        return False
    moment = (now or datetime.now()).time()
    if begin < finish:
        return begin <= moment <= finish
    return moment >= begin or moment <= finish  # 跨午夜区间


# 提示音可选项（key, 显示名）：首次启动时程序合成的圆润卡通音效（wav）。
# 护眼 / 站立 / 坐下三路提醒各自独立选择，默认音色刻意不同；
# 也支持 "file:<绝对路径>" 形式的自定义音效（wav/mp3）。
SOUND_OPTIONS: List[Tuple[str, str]] = [
    ("none", "静音"),
    ("ding", "叮咚"),
    ("pop", "啵灵"),
    ("sparkle", "叮铃"),
    ("chime", "上行三音"),
    ("bell", "清脆双音"),
]
_BUILTIN_SOUNDS = {key for key, _ in SOUND_OPTIONS if key != "none"}

# 合成参数：(起始频率, 结束频率, 时长秒, 音量)。正弦波 + 衰减包络，圆润不刺耳。
_SOUND_DEFS = {
    "ding": [(1318.0, 1318.0, 0.12, 0.50), (0.0, 0.0, 0.04, 0.0),
             (1046.0, 1046.0, 0.26, 0.50)],
    "pop": [(480.0, 1080.0, 0.15, 0.62)],
    "sparkle": [(1568.0, 1568.0, 0.08, 0.40), (1975.0, 1975.0, 0.08, 0.40),
                (2349.0, 2349.0, 0.20, 0.45)],
    "chime": [(523.0, 523.0, 0.12, 0.50), (659.0, 659.0, 0.12, 0.50),
              (784.0, 784.0, 0.26, 0.50)],
    "bell": [(988.0, 988.0, 0.11, 0.50), (1319.0, 1319.0, 0.26, 0.50)],
}


def sound_dir() -> Path:
    """内置合成音效目录：%APPDATA%\\EyeGuard\\sounds。"""
    path = app_data_dir() / "sounds"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def sound_custom_dir() -> Path:
    """用户导入的自定义音效目录。"""
    path = sound_dir() / "custom"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _synth_wav(path: Path, segments) -> None:
    """把 (起始频率, 结束频率, 时秒, 音量) 序列合成为 16bit 单声道 wav。"""
    sample_rate = 22050
    samples: List[float] = []
    for f0, f1, dur, vol in segments:
        count = max(1, int(sample_rate * dur))
        for i in range(count):
            t = i / sample_rate
            k = i / max(1, count - 1)
            freq = f0 + (f1 - f0) * k
            envelope = min(1.0, i / (0.008 * sample_rate)) \
                * math.exp(-3.2 * t / dur)
            value = vol * envelope * (
                math.sin(2 * math.pi * freq * t)
                + 0.20 * math.sin(4 * math.pi * freq * t))
            samples.append(value)
    data = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32000))
        for s in samples)
    with wave_module.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(data)


def ensure_sound_files() -> None:
    """首次运行生成内置卡通音效；已存在则跳过。"""
    for key, segments in _SOUND_DEFS.items():
        target = sound_dir() / f"{key}.wav"
        if not target.exists():
            try:
                _synth_wav(target, segments)
            except OSError as exc:
                LOG.warning("提示音 %s 合成失败: %s", key, exc)


def is_valid_sound(key: str) -> bool:
    """配置里的提示音值是否合法（内置名或 file: 自定义路径）。"""
    return (isinstance(key, str)
            and (key in _BUILTIN_SOUNDS or key == "none"
                 or key.startswith("file:")))


def _play_mci(path: Path) -> None:
    """（兼容入口）经全局单例播放非 wav 音频。"""
    sound_player().play(path)


def play_sound_file(path: Path) -> None:
    """播放音频文件（wav/mp3）；统一走全局单例，失败记录日志。"""
    sound_player().play(path)


class _SoundPlayer:
    """全局单例音频播放器：单一后台工作线程 + “最新优先”指令槽。

    解决“时有时无”的设计：
    1. 进程内唯一实例（sound_player()），播放器常驻，不会反复 new/销毁；
    2. 每次播放等价于先 Stop（新 PlaySound 会停掉上一段；也可显式
       stop()），再加载并 Play——连续点击预览每次都从头出声；
    3. wav 一次性读入内存常驻缓存，由工作线程同步播放
       （winsound 的 SND_MEMORY 不允许与 SND_ASYNC 组合，同步调用必须
       放在后台线程，主界面零阻塞；缓冲被 C 层与常驻缓存双重持有，
       不会被 GC 提前回收）；mp3 等非 wav 在工作线程内走 MCI。
    """

    _ALIAS = "EyeGuardSnd"
    _instance: Optional["_SoundPlayer"] = None
    _initialized = False

    def __new__(cls) -> "_SoundPlayer":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if _SoundPlayer._initialized:
            return
        _SoundPlayer._initialized = True
        self._lock = threading.Lock()
        self._command: Optional[Tuple[str, object]] = None
        self._cond = threading.Condition(self._lock)
        self._volume: float = 0.8   # 默认 80%，永不为 0
        self._wav_cache: Dict[Path, bytes] = {}  # 常驻缓存，防 GC + 防文件锁
        self._ps_proc: Optional[subprocess.Popen] = None
        self._ps_failed = False
        self._worker = threading.Thread(target=self._work, daemon=True,
                                        name="EyeGuard-Audio")
        self._worker.start()

    def _work(self) -> None:
        """工作线程主循环：取最新指令并同步执行（wav 播完才取下一条）。"""
        while True:
            with self._cond:
                while self._command is None:
                    self._cond.wait()
                kind, payload = self._command
                self._command = None
            try:
                self._kill_ps()  # 新指令开始前，结束上一条 PowerShell 播放
                if kind == "stop":
                    self._purge()
                elif kind == "wav":
                    data = self._apply_volume(payload, self._volume)
                    winsound.PlaySound(data, winsound.SND_MEMORY
                                       | winsound.SND_NODEFAULT)
                elif kind == "file":
                    self._play_file(payload)
            except Exception as exc:
                LOG.warning("播放音效失败: %s", exc)

    def _play_file(self, path: Path) -> None:
        """非 wav 音频（mp3/m4a 等）：优先 Media Foundation（对 mp3/m4a
        解码最可靠），失败回退 MCI 并验证真实出声，仍失败回退内置默认音效。"""
        self._ps_play(path)
        if not self._ps_failed:
            return
        LOG.warning("Media Foundation 未能播放 %s，改用 MCI", path.name)
        if self._mci_play(path) and self._mci_audible():
            return
        LOG.warning("MCI 亦未出声，回退内置默认音效")
        self._submit(("wav", self._builtin_bytes("ding")))

    def _builtin_bytes(self, key: str) -> bytes:
        """读取内置音效字节（经常驻缓存）。"""
        return self._wav_bytes(sound_dir() / f"{key}.wav")

    def _mci_audible(self) -> bool:
        """验证 MCI 真正出声：模式为 playing 且播放位置持续推进（≤800ms）。

        仅凭 mode==playing 不足以判断（个别编解码会“打开成功但无声”），
        因此同时检查播放位置是否随时间推进。
        """
        winmm = ctypes.windll.winmm
        buf = ctypes.create_unicode_buffer(64)
        last = -1
        for _ in range(8):
            if winmm.mciSendStringW(f"status {self._ALIAS} mode",
                                    buf, 64, 0) != 0:
                return False
            if buf.value != "playing":
                return False
            if winmm.mciSendStringW(f"status {self._ALIAS} position",
                                    buf, 64, 0) == 0:
                try:
                    position = int(buf.value)
                except ValueError:
                    return False
                if last >= 0 and position > last:
                    return True
                last = position
            time.sleep(0.1)
        return False

    def _ps_play(self, path: Path) -> None:
        """经 PowerShell(WPF MediaPlayer/Media Foundation) 播放 m4a 等格式。

        进程播完自退；下一条指令到达时会先被 _kill_ps 终止（最新优先）。
        进程在 0.6s 内即退出视为解码失败（_ps_failed=True）。
        """
        self._kill_ps()
        self._ps_failed = False
        safe = str(path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName PresentationCore;"
            "$p = New-Object System.Windows.Media.MediaPlayer;"
            f"$p.Open([Uri]::new('{safe}'));"
            "$t = 0;"
            "while (-not $p.NaturalDuration.HasTimeSpan -and $t -lt 80) "
            "{ Start-Sleep -Milliseconds 100; $t++ };"
            "if ($p.NaturalDuration.HasTimeSpan) {"
            f"  $p.Volume = {self._volume:.2f};"
            "  $p.Position = [TimeSpan]::Zero;"
            "  $p.Play();"
            "  $sec = [math]::Ceiling($p.NaturalDuration.TimeSpan.TotalSeconds)"
            " + 1; if ($sec -gt 300) { $sec = 300 };"
            "  Start-Sleep -Seconds $sec"
            "}")
        try:
            self._ps_proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-STA", "-NonInteractive",
                 "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)  # CREATE_NO_WINDOW
        except OSError as exc:
            LOG.warning("Media Foundation 播放启动失败: %s", exc)
            self._ps_failed = True
            return
        time.sleep(0.6)
        if self._ps_proc.poll() is not None:
            self._ps_failed = True

    def _kill_ps(self) -> None:
        """终止残留的 PowerShell 播放进程。"""
        if self._ps_proc is not None and self._ps_proc.poll() is None:
            self._ps_proc.kill()
        self._ps_proc = None

    @staticmethod
    def _apply_volume(data: bytes, volume: float) -> bytes:
        """对 16bit PCM wav 做音量缩放；解析失败原样返回。"""
        if volume >= 0.999:
            return data
        try:
            src = wave_module.open(io.BytesIO(data), "rb")
            if src.getsampwidth() != 2:
                src.close()
                return data
            params = src.getparams()
            frames = src.readframes(src.getnframes())
            src.close()
            samples = array.array("h")
            samples.frombytes(frames)
            for i in range(len(samples)):
                value = int(samples[i] * volume)
                samples[i] = max(-32768, min(32767, value))
            out = io.BytesIO()
            dst = wave_module.open(out, "wb")
            dst.setnchannels(params.nchannels)
            dst.setsampwidth(2)
            dst.setframerate(params.framerate)
            dst.writeframes(samples.tobytes())
            dst.close()
            return out.getvalue()
        except Exception as exc:
            LOG.debug("音量缩放失败，按原音量播放: %s", exc)
            return data

    def _purge(self) -> None:
        """停止本进程当前正在播放的音效。"""
        self._kill_ps()
        if winsound is not None:
            winsound.PlaySound(None, winsound.SND_PURGE)
        if sys.platform == "win32":
            ctypes.windll.winmm.mciSendStringW(f"close {self._ALIAS}",
                                               None, 0, 0)

    def _mci_play(self, path: Path) -> bool:
        """尝试用 Windows MCI 播放；成功提交播放返回 True，失败返回 False。"""
        winmm = ctypes.windll.winmm
        winmm.mciSendStringW(f"close {self._ALIAS}", None, 0, 0)
        rc = winmm.mciSendStringW(
            f'open "{path}" type mpegvideo alias {self._ALIAS}', None, 0, 0)
        if rc != 0:  # 个别编解码用 waveaudio 打开
            rc = winmm.mciSendStringW(f'open "{path}" alias {self._ALIAS}',
                                      None, 0, 0)
        if rc != 0:
            return False
        winmm.mciSendStringW(f"play {self._ALIAS}", None, 0, 0)
        if self._volume < 0.999:  # MCI 音量范围 0-1000
            winmm.mciSendStringW(
                f"setaudio {self._ALIAS} volume to "
                f"{int(self._volume * 1000)}", None, 0, 0)
        return True

    def set_volume(self, percent: int) -> None:
        """设置播放音量（0-100）。默认 80，程序内没有任何静音逻辑。"""
        self._volume = max(0, min(100, int(percent))) / 100.0

    def _wav_bytes(self, path: Path) -> bytes:
        data = self._wav_cache.get(path)
        if data is None:
            data = path.read_bytes()
            self._wav_cache[path] = data
        return data

    def _submit(self, command: Tuple[str, object]) -> None:
        """提交播放指令：直接覆盖未执行的旧指令 → 最新优先。"""
        with self._cond:
            self._command = command
            self._cond.notify()

    def play(self, path: Path) -> None:
        """播放指定音频文件；wav 走内存通道，mp3 等走 MCI。"""
        if winsound is None or sys.platform != "win32":
            return
        if not path.exists():
            LOG.warning("音效文件不存在，跳过播放: %s", path)
            return
        LOG.info("播放音效: %s（音量 %d%%）", path.name,
                 round(self._volume * 100))
        if path.suffix.lower() == ".wav":
            self._submit(("wav", self._wav_bytes(path)))
        else:
            self._submit(("file", path))

    def stop(self) -> None:
        """停止当前正在播放的音效（桌宠关闭/跳过/推迟时调用）。"""
        LOG.info("停止音效")
        self._submit(("stop", None))


def sound_player() -> _SoundPlayer:
    """获取全局唯一音频播放器实例。"""
    return _SoundPlayer()


def play_sound(key: str) -> None:
    """按配置键播放提示音；自定义文件缺失时回退内置“叮咚”，绝不静默无声。"""
    if not key or key == "none":
        return
    if key.startswith("file:"):
        path = Path(key[5:])
        if not path.exists():
            LOG.warning("自定义音效文件不存在，回退默认音效: %s", key)
            play_sound("ding")
            return
        sound_player().play(path)
        return
    play_sound_file(sound_dir() / f"{key}.wav")


def verify_audio_resources(required: Tuple[str, ...] = ("ding", "pop",
                                                        "sparkle")) -> None:
    """启动时检查内置音效资源是否真实存在并记录日志（三路默认音）。"""
    ensure_sound_files()
    missing = [k for k in required
               if not (sound_dir() / f"{k}.wav").exists()]
    if missing:
        LOG.error("内置音效缺失: %s（可删除目录 %s 后重启程序重新生成）",
                  missing, sound_dir())
    else:
        LOG.info("音频资源检查通过: %s（目录 %s）",
                 ", ".join(required), sound_dir())


def build_app_icon() -> QIcon:
    """用 QPainter 绘制“眼睛”图标，托盘/exe 无需任何外部图片文件。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    eye = QPainterPath(QPointF(4.0, 32.0))
    eye.quadTo(QPointF(32.0, 6.0), QPointF(60.0, 32.0))
    eye.quadTo(QPointF(32.0, 58.0), QPointF(4.0, 32.0))
    painter.setPen(QPen(QColor("#37474F"), 3))
    painter.setBrush(QBrush(QColor("#ECEFF1")))
    painter.drawPath(eye)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#26A69A")))
    painter.drawEllipse(QRectF(22.0, 22.0, 20.0, 20.0))
    painter.setBrush(QBrush(QColor("#102027")))
    painter.drawEllipse(QRectF(27.0, 27.0, 10.0, 10.0))
    painter.setBrush(QBrush(QColor("#FFFFFF")))
    painter.drawEllipse(QRectF(29.0, 25.0, 4.0, 4.0))
    painter.end()
    return QIcon(pixmap)
