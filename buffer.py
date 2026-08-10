"""リプレイバッファ: 映像セグメント + 音声リングバッファ。"""

import os
import subprocess
import tempfile
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

from capture import (
    CaptureTarget,
    build_capture_fallbacks,
    is_valid_gdigrab_region,
    refresh_capture_target,
    resolve_capture_target,
    target_needs_restart,
)
from file_acl import ensure_dir_user_can_access, ensure_user_can_access
from process_util import kill_process_tree
from ffmpeg_util import (
    CREATE_NO_WINDOW,
    build_encode_plans,
    encoder_extra_args,
    find_ffmpeg,
    get_loopback_devices,
    WASAPI_PREFIX,
)

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000


class AudioRingBuffer:
    """WASAPI ループバック PCM を時間付きで保持する。"""

    def __init__(self, buffer_seconds: float):
        self.buffer_seconds = buffer_seconds
        self._chunks: list[tuple[float, bytes]] = []
        self._lock = threading.Lock()
        self.sample_rate = 48000
        self.channels = 2
        self._started = False

    def add(self, t: float, data: bytes) -> None:
        with self._lock:
            self._chunks.append((t, data))
            cutoff = t - self.buffer_seconds
            self._chunks = [(ts, d) for ts, d in self._chunks if ts >= cutoff]

    def write_wav(self, path: Path, start: float, end: float) -> bool:
        with self._lock:
            pcm = b"".join(d for ts, d in self._chunks if start <= ts <= end)
        if len(pcm) < 4:
            return False
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm)
        return True

    @property
    def has_data(self) -> bool:
        with self._lock:
            return len(self._chunks) > 0


class ReplayBuffer:
    def __init__(
        self,
        buffer_seconds: int,
        capture_mode: str,
        game_exe: str,
        framerate: int,
        crf: int,
        preset: str,
        audio_device: str | None,
        log: Callable[[str], None] = print,
    ):
        self.buffer_seconds = max(3, min(120, buffer_seconds))
        self.capture_mode = capture_mode
        self.game_exe = game_exe
        self.framerate = framerate
        self.crf = crf
        self.preset = preset
        self.audio_device = audio_device
        self.log = log

        self._segment_dir = Path(tempfile.mkdtemp(prefix="instant_clip_"))
        self._proc: subprocess.Popen | None = None
        self._stderr_file = None
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._region_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._audio_stop = threading.Event()
        self._audio_ring = AudioRingBuffer(self.buffer_seconds + 2.0)
        self._current_target: CaptureTarget | None = None
        self._last_observed_pos: tuple[int, int] | None = None
        self._running = False
        self._capture_ok = False
        self._save_lock = threading.Lock()
        self._save_busy = threading.Lock()
        self._capture_epoch = 0
        self._active_epoch = 0

    @property
    def is_running(self) -> bool:
        return self._running and self._capture_ok

    def start(self) -> None:
        if self._running:
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise FileNotFoundError("ffmpeg.exe が見つかりません。ffmpeg/ フォルダを確認してください。")

        self._running = True
        self._stop_event.clear()
        self._last_observed_pos = None
        self._current_target = resolve_capture_target(
            self.capture_mode,
            self.game_exe,
            log=self.log,
            gfxcapture_available=False,
        )
        self._capture_ok = self._start_video_capture(ffmpeg)
        if not self._capture_ok:
            self._running = False
            raise RuntimeError("FFmpeg の起動に失敗しました")
        if self.audio_device:
            self._start_audio_capture()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        if self.capture_mode in ("auto", "game_window"):
            self._region_thread = threading.Thread(target=self._region_monitor_loop, daemon=True)
            self._region_thread.start()
        self.log(f"バッファ開始 ({self.buffer_seconds}秒)")

    def stop(self) -> None:
        with self._save_lock:
            was_active = self._running or (self._proc and self._proc.poll() is None)
            self._running = False
            self._capture_ok = False
            self._stop_event.set()
            self._audio_stop.set()

            self._kill_ffmpeg()

        if self._stderr_file:
            try:
                self._stderr_file.close()
            except Exception:
                pass
            self._stderr_file = None

        if self._audio_thread and self._audio_thread.is_alive():
            self._audio_thread.join(timeout=5)
        self._audio_thread = None

        for th in (self._cleanup_thread, self._region_thread):
            if th and th.is_alive():
                th.join(timeout=2)
        self._cleanup_thread = None
        self._region_thread = None

        if self._segment_dir.exists():
            self._cleanup_segments(force_all=True)
            try:
                self._segment_dir.rmdir()
            except Exception:
                pass

        if was_active:
            self.log("バッファ停止")

    def _kill_ffmpeg(self, *, flush: bool = False) -> None:
        proc = self._proc
        if proc is None:
            return
        pid = proc.pid
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                proc.wait(timeout=4)
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            if proc.poll() is None:
                kill_process_tree(pid)
        self._proc = None
        if flush:
            time.sleep(0.12)

    def _wait_for_segment_flush(self, timeout: float = 0.8) -> None:
        """FFmpeg 停止後、最終セグメントの書き込み完了を待つ。"""
        deadline = time.time() + timeout
        last_size = -1
        stable_since = time.time()
        while time.time() < deadline:
            files = [f for f in self._iter_segment_files() if f.stat().st_size > 0]
            if not files:
                time.sleep(0.05)
                continue
            newest = max(files, key=lambda f: (f.stat().st_mtime, f.name))
            try:
                size = newest.stat().st_size
            except OSError:
                time.sleep(0.05)
                continue
            if size == last_size:
                if time.time() - stable_since >= 0.12:
                    return
            else:
                last_size = size
                stable_since = time.time()
            time.sleep(0.05)

    def _next_segment_pattern(self) -> str:
        """キャプチャ再起動ごとに別プレフィックスを使い、セグメント上書きを防ぐ。"""
        self._capture_epoch += 1
        return str(self._segment_dir / f"e{self._capture_epoch:05d}_%09d.mp4")

    def _iter_segment_files(self):
        return self._segment_dir.glob("e*_*.mp4")

    def _collect_complete_segments(self, cutoff: float, now: float) -> list[Path]:
        """書き込み完了済みのセグメントのみ収集（現在書き込み中のファイルを除外）。"""
        files = [
            f for f in self._iter_segment_files()
            if f.stat().st_size > 0 and f.stat().st_mtime >= cutoff
        ]
        if not files:
            return []
        # アクティブエポックの最新ファイル（現在 ffmpeg が書き込み中）だけを除外
        current_prefix = f"e{self._active_epoch:05d}_"
        epoch_files = sorted(
            [f for f in files if f.name.startswith(current_prefix)],
            key=lambda f: f.name,
        )
        writing_file = epoch_files[-1] if epoch_files else None
        result = [f for f in files if f is not writing_file]
        return sorted(result, key=lambda f: (f.stat().st_mtime, f.name))

    def _collect_segment_files(
        self, cutoff: float, flushed_epoch: int | None = None
    ) -> list[Path]:
        files = [f for f in self._iter_segment_files() if f.stat().st_size > 0]
        selected = {f for f in files if f.stat().st_mtime >= cutoff}
        if flushed_epoch is not None:
            prefix = f"e{flushed_epoch:05d}_"
            for f in files:
                if f.name.startswith(prefix):
                    selected.add(f)
        return sorted(selected, key=lambda f: (f.stat().st_mtime, f.name))

    def _delete_segment_files(self, files: list[Path]) -> None:
        for f in files:
            try:
                f.unlink()
            except Exception:
                pass

    def save_clip(self, output_dir: Path) -> Path | None:
        if not self._running:
            self.log("[警告] バッファが動作していません")
            return None

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.log("[エラー] ffmpeg が見つかりません")
            return None

        if not self._save_busy.acquire(blocking=False):
            self.log("[警告] クリップ保存処理中です")
            return None

        list_path: Path | None = None
        video_only: Path | None = None
        tmp_final: Path | None = None
        saved_segments: list[Path] = []
        result_path: Path | None = None

        try:
            with self._save_lock:
                now = time.time()
                cutoff = now - self.buffer_seconds
                # キャプチャ継続中: 現在書き込み中のセグメントを除く完了済みファイルのみ収集
                video_files = self._collect_complete_segments(cutoff, now)

                if not video_files:
                    # バッファが溜まっていない場合は従来の方式（ffmpeg停止→全取得）にフォールバック
                    if self._proc_alive():
                        flushed_epoch = self._active_epoch
                        self._kill_ffmpeg(flush=True)
                        self._capture_ok = False
                        self._wait_for_segment_flush()
                        video_files = self._collect_segment_files(cutoff, flushed_epoch)
                    if not video_files:
                        self.log("[警告] 保存できる映像がありません（バッファがまだ溜まっていません）")
                        return None

                ensure_dir_user_can_access(output_dir)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                final_path = output_dir / f"clip_{stamp}.mp4"
                tmp_final = output_dir / f"clip_{stamp}.part"

                list_fd, list_str = tempfile.mkstemp(suffix=".txt", dir=self._segment_dir)
                os.close(list_fd)
                list_path = Path(list_str)
                with list_path.open("w", encoding="utf-8") as lf:
                    for vf in video_files:
                        p = str(vf.resolve()).replace("\\", "/").replace("'", "'\\''")
                        lf.write(f"file '{p}'\n")

                video_only = self._segment_dir / f"_concat_{stamp}.mp4"
                saved_segments = list(video_files)
                # 音声開始時刻を最初のセグメント内容の開始時刻に合わせる
                # セグメントの mtime はそのセグメントの書き込み完了時刻（= 内容の終端）
                # 内容の開始 = mtime - segment_time (0.5s)
                _SEGMENT_TIME = 0.5
                audio_start = video_files[0].stat().st_mtime - _SEGMENT_TIME
                audio_end = now
            # 結合・エンコードはロック外（数十秒かかるため UI をブロックしない）
            self.log("クリップを保存しています…")
            if not self._concat_segments(ffmpeg, list_path, video_only):
                self.log("[エラー] 映像の結合に失敗しました")
                return None

            has_audio = False
            if not self.audio_device:
                pass  # 音声デバイス未設定
            elif not self._audio_ring.has_data:
                self.log("[診断] 音声バッファが空です（音声キャプチャが未起動の可能性）")
            else:
                with self._save_lock:
                    has_audio = self._audio_ring.write_wav(
                        self._segment_dir / "_clip_audio.wav", audio_start, audio_end
                    )
                if not has_audio:
                    self.log("[診断] 指定期間の音声データがありません（VB-Cableに音声が届いていない可能性）")

            if has_audio:
                wav_path = self._segment_dir / "_clip_audio.wav"
                merged = self._segment_dir / f"_merged_{stamp}.mp4"
                merge_result = subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-i",
                        str(video_only),
                        "-i",
                        str(wav_path),
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-ac",
                        "2",
                        "-af",
                        "apad",
                        "-shortest",
                        "-movflags",
                        "+faststart",
                        str(merged),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                    timeout=120,
                )
                wav_path.unlink(missing_ok=True)
                if merge_result.returncode == 0 and merged.exists() and merged.stat().st_size > 500:
                    os.replace(str(merged), str(tmp_final))
                    self.log("[診断] 音声マージ成功")
                else:
                    tail = next(
                        (l.strip() for l in reversed(merge_result.stderr.splitlines()) if l.strip()),
                        "不明",
                    )
                    self.log(f"[警告] 音声マージ失敗: {tail}")
                    os.replace(str(video_only), str(tmp_final))
            else:
                os.replace(str(video_only), str(tmp_final))

            os.replace(str(tmp_final), str(final_path))
            ensure_user_can_access(final_path)
            self.log(f"クリップ保存: {final_path}")
            result_path = final_path
        finally:
            if list_path is not None:
                list_path.unlink(missing_ok=True)
            if video_only is not None:
                video_only.unlink(missing_ok=True)
            if tmp_final is not None and tmp_final.exists():
                tmp_final.unlink(missing_ok=True)
            with self._save_lock:
                if saved_segments:
                    self._delete_segment_files(saved_segments)
                if not self._capture_ok:
                    # フォールバックでffmpegを停止した場合のみ再起動
                    self._resume_capture_after_save(ffmpeg)
            self._save_busy.release()

        return result_path

    def _proc_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def try_recover_capture(self, *, blocking: bool = True) -> bool:
        """キャプチャ停止・異常終了時に FFmpeg を再起動する。"""
        if not self._running:
            return False
        if self._proc_alive() and self._capture_ok:
            return True
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return False
        acquired = self._save_lock.acquire(blocking=blocking)
        if not acquired:
            return False
        try:
            if self._proc_alive() and self._capture_ok:
                return True
            locked = self._current_target is not None
            if self._start_video_capture(ffmpeg, locked=locked):
                self.log("映像キャプチャを再開しました")
                return True
        finally:
            self._save_lock.release()
        return False

    def _resume_capture_after_save(self, ffmpeg: str) -> None:
        if not self._running:
            return
        if self._proc_alive():
            self._capture_ok = True
            return
        if self._start_video_capture(ffmpeg, locked=True):
            return
        self.log("[警告] 保存後のキャプチャ再開に失敗しました")
        if self._start_video_capture(ffmpeg, locked=False):
            self.log("映像キャプチャを再開しました（フルフォールバック）")
            return
        self.log("[エラー] キャプチャを再開できませんでした")

    def _concat_segments(self, ffmpeg: str, list_path: Path, out_path: Path) -> bool:
        """セグメント結合。stream copy を優先し、失敗時に再エンコードへフォールバック。"""
        from ffmpeg_util import get_best_encoder

        def _run_concat(extra_input_args: list, encode_args: list, timeout: int) -> bool:
            cmd = [
                ffmpeg, "-y",
                "-f", "concat", "-safe", "0",
                "-probesize", "5000000",
                "-analyzeduration", "2000000",
                *extra_input_args,
                "-fflags", "+genpts",  # -i の前 = INPUT オプション: concat 読み込み時にPTSを再生成
                "-i", str(list_path),
                *encode_args,
                "-movflags", "+faststart",
                str(out_path),
            ]
            try:
                result = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return False
            if (
                result.returncode == 0
                and out_path.exists()
                and out_path.stat().st_size > 500
            ):
                return True
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            return False

        # 1st: stream copy（再エンコードなし・最速）
        if _run_concat([], ["-c:v", "copy"], timeout=30):
            return True
        if out_path.exists():
            out_path.unlink(missing_ok=True)

        # 2nd: HWエンコーダ → libx264 でフォールバック再エンコード
        primary = get_best_encoder(ffmpeg)
        encoders = [primary] if primary != "libx264" else []
        encoders.append("libx264")

        for encoder in encoders:
            if encoder == "libx264":
                enc_args = ["-c:v", "libx264", "-preset", "fast", "-crf", str(self.crf)]
            elif encoder == "h264_nvenc":
                enc_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(self.crf)]
            elif encoder == "h264_amf":
                enc_args = ["-c:v", "h264_amf", "-quality", "balanced",
                            "-rc", "cqp", "-qp_i", str(self.crf), "-qp_p", str(self.crf)]
            elif encoder == "h264_qsv":
                enc_args = ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", str(self.crf)]
            else:
                enc_args = ["-c:v", encoder, "-preset", "fast"]

            if _run_concat([], enc_args, timeout=120):
                return True
            self.log(f"[診断] 再エンコード失敗 ({encoder})、次の方式を試します")

        return False

    def _start_video_capture(self, ffmpeg: str, locked: bool = False) -> bool:
        self._kill_ffmpeg()

        targets: list[CaptureTarget]
        if locked and self._current_target:
            targets = [self._current_target]
        else:
            targets = build_capture_fallbacks(
                self.capture_mode,
                self.game_exe,
                gfxcapture_available=False,
            )

        for target in targets:
            left, top, width, height = target.left, target.top, target.width, target.height
            plans = build_encode_plans(ffmpeg, width, height)

            for plan in plans:
                if plan.vf and (plan.out_width, plan.out_height) != (width, height):
                    self.log(
                        f"[診断] {plan.encoder}: {width}x{height} → "
                        f"{plan.out_width}x{plan.out_height} に縮小してエンコード"
                    )
                else:
                    mode_note = self._target_mode_label(target)
                    self.log(
                        f"[診断] エンコーダー: {plan.encoder} ({mode_note} {width}x{height})"
                    )

                cmd = [ffmpeg, "-y", "-thread_queue_size", "2048"]
                cmd.extend(self._build_input_args(target))
                if plan.vf:
                    cmd.extend(["-vf", plan.vf])
                cmd.extend(["-c:v", plan.encoder])
                cmd.extend(encoder_extra_args(plan.encoder, self.crf, self.preset, self.framerate))
                cmd.extend(
                    [
                        "-pix_fmt",
                        "yuv420p",
                        "-f",
                        "segment",
                        "-segment_time",
                        "0.5",
                        "-segment_format",
                        "mp4",
                    ]
                )
                segment_pattern = self._next_segment_pattern()
                self._active_epoch = self._capture_epoch
                cmd.append(segment_pattern)

                stderr_path = self._segment_dir / "ffmpeg.log"
                if self._stderr_file:
                    try:
                        self._stderr_file.close()
                    except Exception:
                        pass
                self._stderr_file = open(stderr_path, "w", encoding="utf-8", errors="replace")
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=self._stderr_file,
                    creationflags=CREATE_NO_WINDOW | ABOVE_NORMAL_PRIORITY_CLASS,
                )
                time.sleep(0.5)
                if self._proc.poll() is None:
                    self._current_target = target
                    self._capture_ok = True
                    if not locked:
                        self._log_capture_success(target)
                    return True

                self._kill_ffmpeg()
                self._stderr_file.flush()
                err = (
                    stderr_path.read_text(encoding="utf-8", errors="replace")
                    if stderr_path.exists()
                    else ""
                )
                tail = err[-400:].strip()
                self.log(f"[警告] {plan.encoder} 起動失敗、別方式を試します")
                if tail:
                    for line in tail.splitlines()[-3:]:
                        self.log(f"  {line}")
                continue

        self.log("[エラー] 利用可能なエンコーダーで FFmpeg を起動できませんでした")
        self._capture_ok = False
        return False

    def _target_mode_label(self, target: CaptureTarget) -> str:
        if target.grabber == "gfxcapture" and target.mode == "hwnd":
            return "gfxcapture HWND"
        if target.grabber == "gfxcapture" and target.mode == "window_exe":
            return "gfxcapture exe"
        if target.label and target.label != "fullscreen":
            return "ゲーム領域"
        return "領域"

    def _log_capture_success(self, target: CaptureTarget) -> None:
        if target.grabber == "gfxcapture" and target.mode == "hwnd":
            self.log(
                f"キャプチャ方式: gfxcapture HWND {target.width}x{target.height} "
                f"(ウィンドウ直接・重なり除外)"
            )
        elif target.grabber == "gfxcapture" and target.mode == "window_exe":
            self.log(
                f"キャプチャ方式: gfxcapture {target.game_exe} {target.width}x{target.height} "
                f"(ウィンドウ直接・重なり除外)"
            )
        elif target.label == "fullscreen":
            self.log(f"キャプチャ方式: フルスクリーン {target.width}x{target.height}")
        else:
            self.log(
                f"キャプチャ方式: ゲーム領域 {target.width}x{target.height} (重なりに注意)"
            )

    def _build_input_args(self, target: CaptureTarget) -> list[str]:
        if target.grabber == "gfxcapture":
            opts = [
                f"max_framerate={self.framerate}",
                "capture_cursor=0",
                f"width={target.width}",
                f"height={target.height}",
            ]
            if target.mode == "hwnd" and target.hwnd:
                opts.append(f"hwnd={target.hwnd}")
            elif target.mode == "window_exe" and target.game_exe:
                opts.append(f"window_exe={target.game_exe}")
            lavfi = "gfxcapture=" + ":".join(opts)
            return ["-f", "lavfi", "-i", lavfi, "-an"]

        cmd = [
            "-f",
            "gdigrab",
            "-draw_mouse",
            "0",
            "-framerate",
            str(self.framerate),
        ]
        cmd.extend(
            [
                "-offset_x",
                str(target.left),
                "-offset_y",
                str(target.top),
                "-video_size",
                f"{target.width}x{target.height}",
            ]
        )
        cmd.extend(["-i", "desktop", "-an"])
        return cmd

    def _start_audio_capture(self) -> None:
        if not self.audio_device or not self.audio_device.startswith(WASAPI_PREFIX):
            return

        self._audio_stop.clear()
        device_label = self.audio_device[len(WASAPI_PREFIX):]
        ring = self._audio_ring
        stop = self._audio_stop
        log = self.log

        def _run():
            try:
                import pyaudiowpatch as pyaudio

                pa = pyaudio.PyAudio()
                device_info = None
                try:
                    for info in pa.get_loopback_device_info_generator():
                        if info.get("name", "") == device_label:
                            device_info = info
                            break
                    if device_info is None:
                        for info in pa.get_loopback_device_info_generator():
                            device_info = info
                            break
                except Exception as e:
                    log(f"[警告] ループバック列挙エラー: {e}")

                if device_info is None:
                    log("[警告] 音声デバイスが見つかりません")
                    pa.terminate()
                    return

                sample_rate = int(device_info["defaultSampleRate"])
                native_ch = int(
                    device_info.get("maxInputChannels")
                    or device_info.get("maxOutputChannels")
                    or 2
                )
                device_index = int(device_info["index"])
                ch = 2
                if native_ch > 2:
                    try:
                        probe = pa.open(
                            format=pyaudio.paInt16,
                            channels=2,
                            rate=sample_rate,
                            frames_per_buffer=512,
                            input=True,
                            input_device_index=device_index,
                        )
                        probe.close()
                    except Exception:
                        ch = native_ch

                ring.sample_rate = sample_rate
                ring.channels = ch
                log(f"音声キャプチャ: {device_info.get('name', '?')}")

                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=ch,
                    rate=sample_rate,
                    frames_per_buffer=512,
                    input=True,
                    input_device_index=device_index,
                )
                stream.start_stream()
                while not stop.is_set():
                    try:
                        data = stream.read(512, exception_on_overflow=False)
                        ring.add(time.time(), data)
                    except Exception:
                        break
                stream.stop_stream()
                stream.close()
                pa.terminate()
            except ImportError:
                log("[警告] pyaudiowpatch が必要です: pip install pyaudiowpatch")
            except Exception as e:
                log(f"[警告] 音声キャプチャエラー: {e}")

        self._audio_thread = threading.Thread(target=_run, daemon=True)
        self._audio_thread.start()

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(2.0):
            self._cleanup_segments()

    def _cleanup_segments(self, force_all: bool = False) -> None:
        if force_all:
            cutoff = time.time() + 1
        else:
            cutoff = time.time() - self.buffer_seconds - 2
        for f in self._iter_segment_files():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass

    def _region_monitor_loop(self) -> None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return
        while not self._stop_event.wait(2.0):
            if not self._running:
                continue
            if not self._proc_alive() or not self._capture_ok:
                if self._current_target and self.try_recover_capture(blocking=False):
                    continue
            if not self._capture_ok or not self._proc_alive():
                continue
            if not self._current_target:
                continue
            if not self._save_lock.acquire(blocking=False):
                continue
            try:
                new_target = refresh_capture_target(
                    self._current_target,
                    self.capture_mode,
                    self.game_exe,
                )
                if new_target is None:
                    continue
                if not target_needs_restart(self._current_target, new_target):
                    continue
                if new_target.grabber == "gdigrab" and new_target.mode == "region":
                    if not is_valid_gdigrab_region(
                        new_target.left, new_target.top,
                        new_target.width, new_target.height,
                    ):
                        continue
                    # 誤検出（別ウィンドウを一瞬拾う等）による瞬間的な飛びは無視するが、
                    # ウィンドウを実際に動かした場合は次のポーリングで新しい座標が
                    # 安定するので、その時点で追従できるよう直近の観測値と比較する。
                    prev_pos = self._last_observed_pos
                    new_pos = (new_target.left, new_target.top)
                    self._last_observed_pos = new_pos
                    if prev_pos is not None:
                        jump = max(
                            abs(new_pos[0] - prev_pos[0]),
                            abs(new_pos[1] - prev_pos[1]),
                        )
                        if jump > 300:
                            continue
                prev_target = self._current_target
                self._current_target = new_target
                self.log(
                    f"キャプチャ領域を更新しました "
                    f"({new_target.left},{new_target.top} {new_target.width}x{new_target.height})"
                )
                ok = self._start_video_capture(ffmpeg, locked=True)
                if not ok:
                    self._current_target = prev_target
                    ok = self._start_video_capture(ffmpeg, locked=True)
                    if not ok:
                        self.log("[警告] 領域更新をスキップしました（録画は継続）")
            except Exception:
                pass
            finally:
                self._save_lock.release()
