"""設定の読み書き。"""

import json
import sys
from pathlib import Path

APP_NAME = "InstantClip"
APP_VERSION = "1.0.0"

DEFAULTS: dict = {
    "buffer_seconds": 10,
    "hotkey_vk": 0x78,       # F9
    "hotkey_mod": 0x0002,    # win32con.MOD_CONTROL — 単独F9は他アプリと競合しやすい
    "capture_mode": "auto",  # auto | fullscreen | game_window
    "game_exe": "th123.exe",
    "output_dir": "",
    "framerate": 30,
    "crf": 23,
    "preset": "ultrafast",
    "audio_device_name": "",   # ループバックデバイス名 (空 = 音声なし)
    "game_audio_only": True,   # 後方互換
    "auto_route_audio": True,
}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def config_path() -> Path:
    return _base_dir() / "config.json"


def default_output_dir() -> Path:
    custom = load().get("output_dir", "")
    if custom:
        return Path(custom)
    return Path.home() / "Videos" / "InstantClip"


def load() -> dict:
    path = config_path()
    data = dict(DEFAULTS)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    if not data.get("hotkey_vk"):
        data["hotkey_vk"] = DEFAULTS["hotkey_vk"]
        data["hotkey_mod"] = DEFAULTS["hotkey_mod"]
    return data


def save(data: dict) -> None:
    merged = dict(DEFAULTS)
    merged.update(data)
    config_path().write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
