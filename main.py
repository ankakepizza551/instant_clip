"""InstantClip — ホットキーで直前N秒をクリップ保存。"""

import atexit
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import win32con

import audio_routing
import capture
import config as cfg_mod
from buffer import ReplayBuffer
from ffmpeg_util import find_ffmpeg, get_loopback_devices
from trim_window import TrimWindow
from hotkey import (
    get_tk_hwnd_candidates,
    mod_label,
    try_register_hotkey,
    vk_name,
)
from icon_assets import ICON_ICO, load_icon
from ui_theme import (
    ACCENT,
    ACCENT_HOVER,
    BG,
    BORDER,
    CARD,
    CARD_ALT,
    CORNER,
    CORNER_SM,
    FONT_FAMILY,
    MUTED,
    PAD,
    SECONDARY,
    SUCCESS,
    SUCCESS_SOFT,
    TEAL,
    TEXT,
)

_active_buffer: ReplayBuffer | None = None


def _atexit_cleanup() -> None:
    global _active_buffer
    if _active_buffer is not None:
        try:
            _active_buffer.stop()
        except Exception:
            pass
        _active_buffer = None


atexit.register(_atexit_cleanup)

try:
    import pystray
    from PIL import Image, ImageDraw

    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False

APP_TITLE = "InstantClip"
WIDTH, HEIGHT = 560, 800

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

try:
    _font_cfg = ctk.ThemeManager.theme["CTkFont"]
    if isinstance(_font_cfg, dict) and "Windows" in _font_cfg:
        _font_cfg["Windows"]["family"] = FONT_FAMILY
    elif isinstance(_font_cfg, dict):
        _font_cfg["family"] = FONT_FAMILY
except Exception:
    pass

_NO_AUDIO = "なし（音声なし）"
_VB_KEYWORDS = ("VB-Audio", "CABLE")

HOTKEY_CHOICES = [
    ("Ctrl+F9 (推奨)", 0x78, win32con.MOD_CONTROL),
    ("F9", 0x78, 0),
    ("F10", 0x79, 0),
    ("F11", 0x7A, 0),
    ("F12", 0x7B, 0),
    ("Alt+F9", 0x78, win32con.MOD_ALT),
    ("Ctrl+F10", 0x79, win32con.MOD_CONTROL),
]

CAPTURE_MODES = [
    ("自動 (ゲーム優先)", "auto"),
    ("フルスクリーン", "fullscreen"),
    ("ゲームウィンドウ", "game_window"),
]


class GameAudioRouter:
    """ゲーム起動中は VB-Cable へルーティングし、終了時に元のデバイスへ復元する。"""

    def __init__(self, game_exe: str, enabled: bool, log_q: queue.Queue):
        self.game_exe = game_exe
        self.enabled = enabled
        self.log_q = log_q
        self._was_running = False
        self._prev_device: str | None = None

    def poll(self) -> None:
        if not self.enabled:
            return
        running = capture.is_game_running(self.game_exe)
        if running and not self._was_running:
            # ゲーム起動: VB-Cable へ切替
            vb = audio_routing.find_vbcable()
            if vb:
                self._prev_device = audio_routing.get_default_playback_device_id()
                if audio_routing.set_default_playback_device(vb[1]):
                    self.log_q.put(f"ゲーム検出: 音声出力を「{vb[0]}」に切替")
                else:
                    self._prev_device = None
            else:
                self.log_q.put("[警告] VB-Cable が見つかりません")
        elif not running and self._was_running:
            # ゲーム終了: 元のデバイスへ復元
            if self._prev_device:
                if audio_routing.set_default_playback_device(self._prev_device):
                    self.log_q.put("ゲーム終了: 音声出力を元のデバイスに復元しました")
                self._prev_device = None
        self._was_running = running


class InstantClipApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(f"{WIDTH}x{HEIGHT}")
        self.minsize(WIDTH, HEIGHT)
        self.configure(fg_color=BG)
        if ICON_ICO.exists():
            try:
                self.iconbitmap(str(ICON_ICO))
            except Exception:
                pass

        self.cfg = cfg_mod.load()
        self.log_q: queue.Queue = queue.Queue()
        self.buffer: ReplayBuffer | None = None
        self.hotkey = None
        self._last_hotkey_at = 0.0
        self._hotkey_lock = threading.Lock()
        self._tray_icon = None
        self._closing = False
        self._last_clip_path: Path | None = None
        _audio_dev = self.cfg.get("audio_device_name", "")
        _is_vb = any(kw in _audio_dev for kw in _VB_KEYWORDS)
        self._router = GameAudioRouter(
            self.cfg["game_exe"],
            _is_vb and bool(self.cfg.get("auto_route_audio")),
            self.log_q,
        )
        self._build_ui()
        self._apply_cfg_to_ui()
        self.after(200, self._poll_log)
        self.after(500, self._poll_router)
        self.after(800, self._setup_hotkey)

        if _TRAY_AVAILABLE:
            self._setup_tray()
            self.protocol("WM_DELETE_WINDOW", self._ask_quit_or_tray)
        else:
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        if find_ffmpeg():
            self.after(800, self._start_buffer)

    def _setup_hotkey(self):
        hwnds = get_tk_hwnd_candidates(self)
        self.hotkey, err = try_register_hotkey(
            hwnds,
            self.cfg["hotkey_vk"],
            self.cfg["hotkey_mod"],
            self._on_hotkey,
        )
        if self.hotkey:
            old_vk, old_mod = self.cfg.get("hotkey_vk"), self.cfg.get("hotkey_mod")
            vk = self.hotkey.vk
            mod = self.hotkey.modifiers
            self.cfg["hotkey_vk"] = vk
            self.cfg["hotkey_mod"] = mod
            hk = f"{mod_label(mod)}+{vk_name(vk)}".strip("+")
            backend = getattr(self.hotkey, "_backend", "?")
            self._log(f"ホットキー登録: {hk} ({backend})")
            if (vk, mod) != (old_vk, old_mod):
                cfg_mod.save(self.cfg)
                self._apply_cfg_to_ui()
                self._log("ゲーム競合回避のためホットキー設定を更新しました")
            if backend == "register_hotkey":
                self.after(50, self._poll_hotkey)
            elif backend == "keyboard":
                self._log(
                    "※ ゲーム中に反応しない場合は設定で Ctrl+F9 を選び、"
                    "保存後に (thread) 表示を確認してください"
                )
        else:
            self._log(f"[警告] ホットキー登録失敗: {err}")
            messagebox.showwarning(
                APP_TITLE,
                f"ホットキーの登録に失敗しました。\n{err}\n\n"
                "・InstantClip の二重起動を終了してください\n"
                "・設定で Ctrl+F10 等に変更してください",
            )

    def _poll_hotkey(self):
        if self.hotkey:
            self.hotkey.poll()
        self.after(50, self._poll_hotkey)

    def _font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)

    def _card(self, parent, **kwargs) -> ctk.CTkFrame:
        return ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=CORNER,
            border_width=1,
            border_color=BORDER,
            **kwargs,
        )

    def _btn(
        self,
        parent,
        text: str,
        command,
        *,
        accent: bool = False,
        height: int = 38,
    ) -> ctk.CTkButton:
        if accent:
            return ctk.CTkButton(
                parent,
                text=text,
                command=command,
                height=height,
                corner_radius=CORNER_SM,
                font=self._font(13, "bold"),
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                text_color="#ffffff",
            )
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            height=height,
            corner_radius=CORNER_SM,
            font=self._font(12),
            fg_color=SECONDARY,
            hover_color=BORDER,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
        )

    def _set_status(self, text: str, recording: bool = False) -> None:
        self.status_label.configure(text=text)
        if recording:
            self.status_pill.configure(fg_color=SUCCESS_SOFT, border_color=SUCCESS)
            self.status_dot.configure(text_color=SUCCESS)
        else:
            self.status_pill.configure(fg_color=CARD_ALT, border_color=BORDER)
            self.status_dot.configure(text_color=MUTED)

    def _build_ui(self):
        root_pad = {"padx": PAD, "pady": (PAD, 6)}

        header = self._card(self)
        header.pack(fill="x", **root_pad)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x", padx=14, pady=(14, 8))

        ctk.CTkLabel(
            title_row,
            text="InstantClip",
            font=self._font(22, "bold"),
            text_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            title_row,
            text=f"v{cfg_mod.APP_VERSION}",
            font=self._font(11),
            text_color=MUTED,
        ).pack(side="left", padx=(10, 0), pady=(6, 0))

        self.status_pill = ctk.CTkFrame(
            header,
            fg_color=CARD_ALT,
            corner_radius=20,
            border_width=1,
            border_color=BORDER,
        )
        self.status_pill.pack(fill="x", padx=14, pady=(0, 14))

        status_inner = ctk.CTkFrame(self.status_pill, fg_color="transparent")
        status_inner.pack(fill="x", padx=12, pady=8)

        self.status_dot = ctk.CTkLabel(
            status_inner,
            text="●",
            font=self._font(12),
            text_color=MUTED,
            width=16,
        )
        self.status_dot.pack(side="left")
        self.status_label = ctk.CTkLabel(
            status_inner,
            text="準備中...",
            font=self._font(12),
            text_color=TEXT,
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        settings = self._card(self)
        settings.pack(fill="x", padx=PAD, pady=6)
        settings.columnconfigure(1, weight=1)

        cell = {"padx": 12, "pady": 6}
        lbl_style = {"font": self._font(12), "text_color": MUTED}

        ctk.CTkLabel(settings, text="バッファ秒数", **lbl_style).grid(
            row=0, column=0, sticky="w", **cell
        )
        self.buffer_spin = ctk.CTkEntry(
            settings, width=90, height=32, corner_radius=CORNER_SM,
            fg_color=CARD_ALT, border_color=BORDER, text_color=TEXT,
        )
        self.buffer_spin.grid(row=0, column=1, sticky="w", **cell)

        ctk.CTkLabel(settings, text="ホットキー", **lbl_style).grid(
            row=1, column=0, sticky="w", **cell
        )
        self.hotkey_menu = ctk.CTkOptionMenu(
            settings,
            values=[x[0] for x in HOTKEY_CHOICES],
            width=220,
            height=32,
            corner_radius=CORNER_SM,
            fg_color=CARD_ALT,
            button_color=SECONDARY,
            button_hover_color=BORDER,
            dropdown_fg_color=CARD,
            dropdown_hover_color=SECONDARY,
            text_color=TEXT,
        )
        self.hotkey_menu.grid(row=1, column=1, sticky="w", **cell)

        ctk.CTkLabel(settings, text="キャプチャ", **lbl_style).grid(
            row=2, column=0, sticky="w", **cell
        )
        self.capture_menu = ctk.CTkOptionMenu(
            settings,
            values=[x[0] for x in CAPTURE_MODES],
            width=220,
            height=32,
            corner_radius=CORNER_SM,
            fg_color=CARD_ALT,
            button_color=SECONDARY,
            button_hover_color=BORDER,
            dropdown_fg_color=CARD,
            dropdown_hover_color=SECONDARY,
            text_color=TEXT,
        )
        self.capture_menu.grid(row=2, column=1, sticky="w", **cell)

        ctk.CTkLabel(settings, text="出力フォルダ", **lbl_style).grid(
            row=3, column=0, sticky="w", **cell
        )
        out_row = ctk.CTkFrame(settings, fg_color="transparent")
        out_row.grid(row=3, column=1, sticky="ew", **cell)
        self.out_entry = ctk.CTkEntry(
            out_row,
            height=32,
            corner_radius=CORNER_SM,
            fg_color=CARD_ALT,
            border_color=BORDER,
            text_color=TEXT,
        )
        self.out_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            out_row,
            text="…",
            width=40,
            height=32,
            corner_radius=CORNER_SM,
            fg_color=SECONDARY,
            hover_color=BORDER,
            command=self._pick_output,
        ).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(settings, text="FPS", **lbl_style).grid(
            row=4, column=0, sticky="w", **cell
        )
        self.fps_spin = ctk.CTkEntry(
            settings, width=90, height=32, corner_radius=CORNER_SM,
            fg_color=CARD_ALT, border_color=BORDER, text_color=TEXT,
        )
        self.fps_spin.grid(row=4, column=1, sticky="w", **cell)

        ctk.CTkLabel(settings, text="音声デバイス", **lbl_style).grid(
            row=5, column=0, sticky="w", **cell
        )
        self._audio_device_choices = [_NO_AUDIO] + get_loopback_devices()
        self.audio_menu = ctk.CTkOptionMenu(
            settings,
            values=self._audio_device_choices,
            height=32,
            corner_radius=CORNER_SM,
            fg_color=CARD_ALT,
            button_color=SECONDARY,
            button_hover_color=BORDER,
            dropdown_fg_color=CARD,
            dropdown_hover_color=SECONDARY,
            text_color=TEXT,
        )
        self.audio_menu.grid(row=5, column=1, sticky="ew", **cell)

        opts = ctk.CTkFrame(settings, fg_color="transparent")
        opts.grid(row=6, column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 10))
        self.chk_auto_route = ctk.CTkCheckBox(
            opts,
            text="VB-Cable: ゲーム起動時に自動切替",
            font=self._font(12),
            text_color=TEXT,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            border_color=BORDER,
        )
        self.chk_auto_route.pack(anchor="w", pady=2)
        ctk.CTkLabel(
            opts,
            text="※ VB-Cable のインストールが必要です",
            font=self._font(11),
            text_color=MUTED,
        ).pack(anchor="w", pady=(4, 0))

        actions = self._card(self)
        actions.pack(fill="x", padx=PAD, pady=6)
        for col in range(3):
            actions.columnconfigure(col, weight=1, uniform="act")

        act_pad = {"padx": 6, "pady": 6}
        self._btn(actions, "設定を保存", self._save_settings).grid(
            row=0, column=0, sticky="ew", **act_pad
        )
        self._btn(actions, "バッファ開始", self._start_buffer).grid(
            row=0, column=1, sticky="ew", **act_pad
        )
        self._btn(actions, "バッファ停止", self._stop_buffer).grid(
            row=0, column=2, sticky="ew", **act_pad
        )
        self._btn(
            actions,
            "クリップ保存",
            self._save_clip,
            accent=True,
            height=44,
        ).grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(2, 4))
        self._btn(actions, "切り抜き", self._open_trim).grid(
            row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 8)
        )

        log_card = self._card(self)
        log_card.pack(fill="both", expand=True, padx=PAD, pady=(6, PAD))
        ctk.CTkLabel(
            log_card,
            text="ログ",
            font=self._font(12, "bold"),
            text_color=TEAL,
        ).pack(anchor="w", padx=12, pady=(10, 4))
        self.log_box = ctk.CTkTextbox(
            log_card,
            height=260,
            corner_radius=CORNER_SM,
            fg_color=CARD_ALT,
            border_color=BORDER,
            text_color=TEXT,
            font=self._font(11),
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _apply_cfg_to_ui(self):
        self.buffer_spin.insert(0, str(self.cfg.get("buffer_seconds", 10)))
        self.fps_spin.insert(0, str(self.cfg.get("framerate", 30)))
        out = self.cfg.get("output_dir") or str(cfg_mod.default_output_dir())
        self.out_entry.insert(0, out)

        vk, mod = self.cfg.get("hotkey_vk", 0x78), self.cfg.get("hotkey_mod", 0)
        for label, v, m in HOTKEY_CHOICES:
            if v == vk and m == mod:
                self.hotkey_menu.set(label)
                break

        mode = self.cfg.get("capture_mode", "auto")
        for label, val in CAPTURE_MODES:
            if val == mode:
                self.capture_menu.set(label)
                break

        audio_name = self.cfg.get("audio_device_name", "")
        if not audio_name and self.cfg.get("game_audio_only"):
            vb = audio_routing.find_vbcable_loopback_name()
            if vb:
                audio_name = vb
        if audio_name in self._audio_device_choices:
            self.audio_menu.set(audio_name)
        else:
            self.audio_menu.set(_NO_AUDIO)
        if self.cfg.get("auto_route_audio"):
            self.chk_auto_route.select()

    def _collect_cfg(self) -> dict:
        label = self.hotkey_menu.get()
        vk, mod = 0x78, 0
        for l, v, m in HOTKEY_CHOICES:
            if l == label:
                vk, mod = v, m
                break

        cap_label = self.capture_menu.get()
        capture_mode = "auto"
        for l, v in CAPTURE_MODES:
            if l == cap_label:
                capture_mode = v
                break

        try:
            buffer_seconds = int(self.buffer_spin.get().strip())
        except ValueError:
            buffer_seconds = 10

        try:
            framerate = int(self.fps_spin.get().strip())
        except ValueError:
            framerate = 30

        audio_name = self.audio_menu.get()
        if audio_name == _NO_AUDIO:
            audio_name = ""
        return {
            "buffer_seconds": buffer_seconds,
            "hotkey_vk": vk,
            "hotkey_mod": mod,
            "capture_mode": capture_mode,
            "output_dir": self.out_entry.get().strip(),
            "framerate": framerate,
            "audio_device_name": audio_name,
            "game_audio_only": bool(audio_name),
            "auto_route_audio": bool(self.chk_auto_route.get()),
            "game_exe": self.cfg.get("game_exe", "th123.exe"),
            "crf": self.cfg.get("crf", 23),
            "preset": self.cfg.get("preset", "ultrafast"),
        }

    def _save_settings(self):
        prev = dict(self.cfg)
        self.cfg = self._collect_cfg()
        cfg_mod.save(self.cfg)
        _audio_dev = self.cfg.get("audio_device_name", "")
        _is_vb = any(kw in _audio_dev for kw in _VB_KEYWORDS)
        self._router.enabled = _is_vb and bool(self.cfg.get("auto_route_audio"))
        self._router.game_exe = self.cfg["game_exe"]
        self._reregister_hotkey()
        self._log("設定を保存しました")
        buffer_keys = (
            "buffer_seconds",
            "capture_mode",
            "framerate",
            "game_exe",
            "audio_device_name",
            "game_audio_only",
            "auto_route_audio",
        )
        if self.buffer and self.buffer._running:
            if any(self.cfg.get(k) != prev.get(k) for k in buffer_keys):
                self._stop_buffer()
                self._start_buffer()

    def _pick_output(self):
        d = filedialog.askdirectory()
        if d:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, d)

    def _resolve_audio_device(self) -> str | None:
        name = self.cfg.get("audio_device_name", "")
        return name if name else None

    def _start_buffer(self):
        if not find_ffmpeg():
            self._log("[エラー] ffmpeg.exe が見つかりません")
            self._set_status("ffmpeg 未検出", recording=False)
            return
        if self.buffer and self.buffer._running:
            return

        self.cfg = self._collect_cfg()
        audio_dev = self._resolve_audio_device()
        self.buffer = ReplayBuffer(
            buffer_seconds=self.cfg["buffer_seconds"],
            capture_mode=self.cfg["capture_mode"],
            game_exe=self.cfg.get("game_exe", "th123.exe"),
            framerate=self.cfg.get("framerate", 30),
            crf=self.cfg.get("crf", 23),
            preset=self.cfg.get("preset", "ultrafast"),
            audio_device=audio_dev,
            log=self._log,
        )
        global _active_buffer
        _active_buffer = self.buffer
        try:
            self.buffer.start()
            hk = f"{mod_label(self.cfg['hotkey_mod'])}+{vk_name(self.cfg['hotkey_vk'])}".strip("+")
            self._set_status(
                f"録画中 — {self.cfg['buffer_seconds']}秒バッファ / {hk} でクリップ保存",
                recording=True,
            )
        except Exception as e:
            self._log(f"[エラー] {e}")
            self.buffer = None
            _active_buffer = None

    def _stop_buffer(self):
        global _active_buffer
        if self.buffer:
            self.buffer.stop()
            self.buffer = None
        _active_buffer = None
        self._set_status("停止中", recording=False)

    def _save_clip(self):
        if not self.buffer or not self.buffer._running:
            self._log("[警告] バッファが動作していません")
            return
        out_dir = Path(self.out_entry.get().strip() or cfg_mod.default_output_dir())
        threading.Thread(
            target=self._save_clip_worker,
            args=(out_dir,),
            daemon=True,
        ).start()

    def _on_hotkey(self):
        """任意スレッドから呼ばれる。Tk を経由せず直接保存ワーカーを起動する。"""
        with self._hotkey_lock:
            now = time.time()
            if now - self._last_hotkey_at < 0.8:
                return
            self._last_hotkey_at = now
        out = self.cfg.get("output_dir", "").strip()
        out_dir = Path(out) if out else cfg_mod.default_output_dir()
        threading.Thread(
            target=self._save_clip_worker,
            args=(out_dir,),
            daemon=True,
        ).start()

    def _save_clip_worker(self, out_dir: Path):
        if not self.buffer:
            return
        try:
            if not self.buffer._capture_ok or not self.buffer._proc_alive():
                self.buffer.try_recover_capture()
            path = self.buffer.save_clip(out_dir)
            if path:
                self._last_clip_path = path
                self.log_q.put(f"✓ 保存完了: {path.name}")
        except Exception as e:
            self.log_q.put(f"[エラー] クリップ保存中に例外: {e}")

    def _open_trim(self):
        init_dir = str(self.cfg.get("output_dir") or cfg_mod.default_output_dir())
        init_file = str(self._last_clip_path) if self._last_clip_path and self._last_clip_path.exists() else ""
        path = filedialog.askopenfilename(
            title="切り抜く動画を選択",
            initialdir=init_dir,
            initialfile=init_file,
            filetypes=[("MP4 動画", "*.mp4"), ("すべてのファイル", "*.*")],
        )
        if path:
            TrimWindow(self, path, log=self._log)

    def _reregister_hotkey(self):
        if self.hotkey:
            self.hotkey.stop()
        hwnds = get_tk_hwnd_candidates(self)
        self.hotkey, err = try_register_hotkey(
            hwnds,
            self.cfg["hotkey_vk"],
            self.cfg["hotkey_mod"],
            self._on_hotkey,
        )
        if self.hotkey:
            old_vk, old_mod = self.cfg.get("hotkey_vk"), self.cfg.get("hotkey_mod")
            self.cfg["hotkey_vk"] = self.hotkey.vk
            self.cfg["hotkey_mod"] = self.hotkey.modifiers
            hk = f"{mod_label(self.hotkey.modifiers)}+{vk_name(self.hotkey.vk)}".strip("+")
            backend = getattr(self.hotkey, "_backend", "?")
            self._log(f"ホットキー再登録: {hk} ({backend})")
            if (self.hotkey.vk, self.hotkey.modifiers) != (old_vk, old_mod):
                cfg_mod.save(self.cfg)
                self._apply_cfg_to_ui()
        else:
            self._log(f"[警告] ホットキー再登録に失敗: {err}")

    def _log(self, msg: str):
        self.log_q.put(msg)

    def _poll_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
        except queue.Empty:
            pass
        self.after(200, self._poll_log)

    def _poll_router(self):
        self._router.poll()
        self.after(1000, self._poll_router)

    def _setup_tray(self):
        img = load_icon(64)

        menu = pystray.Menu(
            pystray.MenuItem("表示", self._show_window, default=True),
            pystray.MenuItem("クリップ保存", lambda: self.after(0, self._save_clip)),
            pystray.MenuItem("終了", self._quit_app),
        )
        self._tray_icon = pystray.Icon(APP_TITLE, img, APP_TITLE, menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _hide_to_tray(self):
        if _TRAY_AVAILABLE:
            self.withdraw()
        else:
            self._on_close()

    def _ask_quit_or_tray(self):
        """× ボタン: 終了かバックグラウンド継続かを選べる。"""
        if self._closing:
            return
        if _TRAY_AVAILABLE:
            choice = messagebox.askyesnocancel(
                APP_TITLE,
                "InstantClip を終了しますか？\n\n"
                "はい = 完全終了\n"
                "いいえ = バックグラウンドで継続（タスクバー常駐）\n"
                "キャンセル = 何もしない",
            )
            if choice is True:
                self._on_close()
            elif choice is False:
                self._hide_to_tray()
        else:
            self._on_close()

    def _show_window(self, icon=None, item=None):
        self.after(0, lambda: (self.deiconify(), self.lift()))

    def _quit_app(self, icon=None, item=None):
        self.after(0, self._on_close)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True

        if self.hotkey:
            try:
                self.hotkey.stop()
            except Exception:
                pass
            self.hotkey = None

        self._stop_buffer()

        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None

        try:
            self.destroy()
        except Exception:
            pass


def main():
    if not find_ffmpeg():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            "ffmpeg.exe が見つかりません。\n"
            "instant_clip/ffmpeg/ffmpeg.exe を配置してください。",
        )
        return
    app = InstantClipApp()
    app.mainloop()


if __name__ == "__main__":
    main()
