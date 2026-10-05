"""FFmpeg ユーティリティ。"""

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WASAPI_PREFIX = "[ループバック] "
CREATE_NO_WINDOW = 0x08000000

# NVENC / QSV / AMF の H.264 実用上の上限（幅 4480 等のデュアルモニタで超えやすい）
_HW_MAX_WIDTH = 4096
_HW_MAX_HEIGHT = 4096


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def find_ffmpeg() -> str | None:
    bundled = base_dir() / "ffmpeg" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("ffmpeg")


def _even(n: int) -> int:
    return n if n % 2 == 0 else max(2, n - 1)


_HW_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv")
_best_encoder_cache: dict[str, str] = {}


def _encoder_usable(ffmpeg: str, encoder: str) -> bool:
    """実際に 1 フレームエンコードして、この PC で使えるか確かめる。"""
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=640x360:r=30",
                "-frames:v", "1", "-c:v", encoder, "-f", "null", "-",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
        return proc.returncode == 0
    except Exception:
        return False


def get_best_encoder(ffmpeg: str) -> str:
    """この PC で実際に動く H.264 エンコーダを返す（結果はキャッシュ）。"""
    cached = _best_encoder_cache.get(ffmpeg)
    if cached:
        return cached
    best = "libx264"
    try:
        proc = subprocess.run(
            [ffmpeg, "-encoders"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        # ビルドに含まれていても GPU が無ければ使えないので、試してから決める
        for encoder in _HW_ENCODERS:
            if encoder in proc.stdout and _encoder_usable(ffmpeg, encoder):
                best = encoder
                break
    except Exception:
        pass
    _best_encoder_cache[ffmpeg] = best
    return best


@dataclass
class EncodePlan:
    encoder: str
    out_width: int
    out_height: int
    vf: str | None  # scale フィルタ（不要なら None）


def _fit_hw_encoder(width: int, height: int) -> tuple[int, int, str | None]:
    if width <= _HW_MAX_WIDTH and height <= _HW_MAX_HEIGHT:
        return width, height, None
    scale = min(_HW_MAX_WIDTH / width, _HW_MAX_HEIGHT / height)
    out_w = _even(int(width * scale))
    out_h = _even(int(height * scale))
    return out_w, out_h, f"scale={out_w}:{out_h}"


def build_encode_plans(ffmpeg: str, width: int, height: int) -> list[EncodePlan]:
    """
    キャプチャ解像度に合わせたエンコード計画を返す。
    ハードウェアエンコーダは上限超過時に scale し、失敗時用に libx264 も用意する。
    """
    primary = get_best_encoder(ffmpeg)
    plans: list[EncodePlan] = []

    if primary in ("h264_nvenc", "h264_amf", "h264_qsv"):
        out_w, out_h, vf = _fit_hw_encoder(width, height)
        plans.append(EncodePlan(primary, out_w, out_h, vf))
        plans.append(
            EncodePlan(
                "libx264",
                out_w,
                out_h,
                vf or (f"scale={out_w}:{out_h}" if (out_w, out_h) != (width, height) else None),
            )
        )
    else:
        plans.append(EncodePlan("libx264", width, height, None))

    return plans


def encoder_extra_args(encoder: str, crf: int, preset: str, framerate: int = 30) -> list[str]:
    # セグメントマルチプレクサはキーフレームでしか分割できないため、
    # 0.5s セグメントに合わせてキーフレーム間隔を framerate//2 フレームに強制する
    kf = max(1, framerate // 2)
    if encoder == "libx264":
        effective = preset
        if crf <= 17 and preset not in ("ultrafast", "superfast"):
            effective = "ultrafast"
        return ["-preset", effective, "-tune", "zerolatency", "-crf", str(crf),
                "-g", str(kf), "-keyint_min", str(kf)]
    if encoder == "h264_nvenc":
        # -bf 0: Bフレーム無効 → エンコーダー遅延ゼロ、edit list なし → concat PTS がクリーンになる
        return ["-preset", "p4", "-rc", "vbr", "-cq", str(crf),
                "-g", str(kf), "-forced-idr", "1", "-bf", "0"]
    if encoder == "h264_amf":
        return ["-quality", "balanced", "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf),
                "-g", str(kf), "-bf", "0"]
    if encoder == "h264_qsv":
        return ["-preset", "veryfast", "-global_quality", str(crf),
                "-g", str(kf)]
    return ["-preset", "fast"]


def get_default_loopback_device() -> str | None:
    """既定の出力デバイス（今聞こえている音）のループバック名。"""
    try:
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        try:
            name = pa.get_default_wasapi_loopback().get("name", "")
            return f"{WASAPI_PREFIX}{name}" if name else None
        finally:
            pa.terminate()
    except Exception:
        return None


def get_loopback_devices() -> list[str]:
    devices: list[str] = []
    try:
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        try:
            for info in pa.get_loopback_device_info_generator():
                name = info.get("name", "")
                if name:
                    devices.append(f"{WASAPI_PREFIX}{name}")
        finally:
            pa.terminate()
    except Exception:
        pass
    return devices
