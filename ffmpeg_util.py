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


def has_ffmpeg_filter(ffmpeg: str, filter_name: str) -> bool:
    """ffmpeg -filters に指定フィルタが含まれるか。"""
    try:
        proc = subprocess.run(
            [ffmpeg, "-filters"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
        return filter_name in proc.stdout
    except Exception:
        return False


def _even(n: int) -> int:
    return n if n % 2 == 0 else max(2, n - 1)


def get_best_encoder(ffmpeg: str) -> str:
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
        if "h264_nvenc" in proc.stdout:
            return "h264_nvenc"
        if "h264_amf" in proc.stdout:
            return "h264_amf"
        if "h264_qsv" in proc.stdout:
            return "h264_qsv"
    except Exception:
        pass
    return "libx264"


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
