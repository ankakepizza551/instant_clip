"""設定の読み書き。"""

import json
import os
import sys
from pathlib import Path

APP_NAME = "InstantClip"
APP_VERSION = "1.0.0"

DEFAULTS: dict = {
    "buffer_seconds": 10,
    "hotkey_vk": 0x78,       # F9
    "hotkey_mod": 0x0002,    # win32con.MOD_CONTROL — 単独F9は他アプリと競合しやすい
    "capture_mode": "auto",  # auto | fullscreen | game_window
    "monitor": "primary",    # フルスクリーン時に撮るモニタ: primary | all | デバイス名
    "game_exe": "th123.exe",
    "output_dir": "",
    "framerate": 30,
    "crf": 23,
    "preset": "ultrafast",
    "audio_device_name": "",   # ループバックデバイス名 (空 = 音声なし)
    "game_audio_only": True,   # 後方互換: デバイス名が空でこれが True なら未設定（既定の出力を使う）
    "auto_route_audio": True,
    "notify": "sound",         # 保存結果の通知: sound | toast | none
}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _legacy_config_path() -> Path:
    """旧バージョンの保存場所（exe と同じフォルダ）。"""
    return _base_dir() / "config.json"


def config_path() -> Path:
    # exe と同じフォルダだと再ビルドや更新のたびに消えるため、ユーザーごとの設定フォルダに置く
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home()
    return root / APP_NAME / "config.json"


def default_output_dir() -> Path:
    custom = load().get("output_dir", "")
    if custom:
        return Path(custom)
    return Path.home() / "Videos" / "InstantClip"


def load() -> dict:
    path = config_path()
    if not path.exists() and _legacy_config_path().exists():
        path = _legacy_config_path()
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
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
