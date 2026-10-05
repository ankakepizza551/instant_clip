"""グローバルホットキー — RegisterHotKey 優先（ゲーム中も有効）、keyboard はフォールバック。"""

import ctypes
import threading
from ctypes import wintypes

import win32api
import win32con
import win32gui

MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_DESTROY = 0x0002
HOTKEY_ID = 1
GA_ROOT = 2
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# 64bit Python で GetModuleHandleW / CreateWindowExW の HWND が overflow しないよう型を固定
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
_kernel32.GetLastError.restype = wintypes.DWORD

_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.RegisterHotKey.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.UINT,
]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
_user32.DefWindowProcW.restype = LRESULT

HWND_MESSAGE = wintypes.HWND(-3)

WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def get_tk_hwnd_candidates(root) -> list[int]:
    """CustomTkinter / Tk で RegisterHotKey に使える HWND を列挙する。"""
    root.update_idletasks()
    root.update()
    seen: set[int] = set()
    out: list[int] = []

    def add(h: int) -> None:
        if h and _user32.IsWindow(h) and h not in seen:
            seen.add(h)
            out.append(h)

    add(root.winfo_id())
    add(_user32.GetParent(root.winfo_id()))
    add(_user32.GetAncestor(root.winfo_id(), GA_ROOT))
    title = root.title()
    if title:
        add(_user32.FindWindowW(None, title))
    return out


def get_tk_hwnd(root) -> int:
    candidates = get_tk_hwnd_candidates(root)
    return candidates[0] if candidates else 0


# keyboard パッケージでの呼び名が表示名と違うキー
_KEYBOARD_LIB_NAMES = {
    "PageUp": "page up",
    "PageDown": "page down",
    "PrintScreen": "print screen",
    "ScrollLock": "scroll lock",
}


def vk_mod_to_combo(vk: int, modifiers: int) -> str:
    parts: list[str] = []
    if modifiers & win32con.MOD_CONTROL:
        parts.append("ctrl")
    if modifiers & win32con.MOD_ALT:
        parts.append("alt")
    if modifiers & win32con.MOD_SHIFT:
        parts.append("shift")
    name = vk_name(vk)
    if name.startswith("Num") and name[3:].isdigit():
        parts.append(f"num {name[3:]}")
    else:
        parts.append(_KEYBOARD_LIB_NAMES.get(name, name.lower()))
    return "+".join(parts)


class KeyboardHotkey:
    """keyboard パッケージによるグローバルホットキー（HWND 不要）。"""

    def __init__(self, vk: int, modifiers: int, callback):
        self.vk = vk
        self.modifiers = modifiers
        self.callback = callback
        self._combo = ""
        self._registered = False
        self.last_error = 0
        self._backend = "keyboard"

    @property
    def is_registered(self) -> bool:
        return self._registered

    def poll(self) -> None:
        pass

    def start(self, suppress: bool = False) -> bool:
        try:
            import keyboard
        except ImportError:
            self.last_error = -2
            return False

        self._combo = vk_mod_to_combo(self.vk, self.modifiers)
        try:
            keyboard.add_hotkey(self._combo, self._on_press, suppress=suppress)
            self._registered = True
            return True
        except Exception:
            self.last_error = -1
            self._registered = False
            return False

    def _on_press(self) -> None:
        try:
            self.callback()
        except Exception:
            pass

    def stop(self) -> None:
        if not self._registered:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self._combo)
        except Exception:
            try:
                import keyboard

                keyboard.unhook_all()
            except Exception:
                pass
        self._registered = False

    def update(self, vk: int, modifiers: int) -> bool:
        self.stop()
        self.vk = vk
        self.modifiers = modifiers
        return self.start()


class DualHotkey:
    """RegisterHotKey（スレッド）を優先し、登録できない時だけ keyboard フックを使う。

    keyboard の低レベルフックは全キー入力を Python 経由にして入力遅延の原因になるため、
    RegisterHotKey が成功した場合は張らない。
    """

    def __init__(self, vk: int, modifiers: int, callback):
        self.vk = vk
        self.modifiers = modifiers
        self.callback = callback
        self._thread = ThreadHotkey(vk, modifiers, callback)
        self._keyboard = KeyboardHotkey(vk, modifiers, callback)
        self._registered = False
        self.last_error = 0
        self._backend = "thread+keyboard"
        self._thread_ok = False
        self._keyboard_ok = False

    @property
    def is_registered(self) -> bool:
        return self._registered

    def poll(self) -> None:
        pass

    def start(self) -> bool:
        self._thread_ok = self._thread.start()
        self._keyboard_ok = False if self._thread_ok else self._keyboard.start()
        self._registered = self._thread_ok or self._keyboard_ok
        if self._thread_ok:
            self.vk = self._thread.vk
            self.modifiers = self._thread.modifiers
            self._backend = "thread"
        elif self._keyboard_ok:
            self.vk = self._keyboard.vk
            self.modifiers = self._keyboard.modifiers
            self._backend = "keyboard"
        else:
            self.last_error = self._thread.last_error or self._keyboard.last_error
        return self._registered

    def stop(self) -> None:
        self._thread.stop()
        self._keyboard.stop()
        self._registered = False

    def update(self, vk: int, modifiers: int) -> bool:
        self.stop()
        self.vk = vk
        self.modifiers = modifiers
        self._thread = ThreadHotkey(vk, modifiers, self.callback)
        self._keyboard = KeyboardHotkey(vk, modifiers, self.callback)
        return self.start()


class TkHotkey:
    """RegisterHotKey + PeekMessage（Tk メインスレッド）。"""

    def __init__(self, hwnd: int, vk: int, modifiers: int, callback):
        self.hwnd = hwnd
        self.vk = vk
        self.modifiers = modifiers
        self.callback = callback
        self._registered = False
        self.last_error = 0
        self._backend = "register_hotkey"

    @property
    def is_registered(self) -> bool:
        return self._registered

    def register(self) -> bool:
        if not self.hwnd or not _user32.IsWindow(self.hwnd):
            self.last_error = 1407  # invalid window handle
            return False
        flags = (self.modifiers or 0) | MOD_NOREPEAT
        try:
            ok = win32gui.RegisterHotKey(self.hwnd, HOTKEY_ID, flags, self.vk)
            self._registered = bool(ok)
            self.last_error = win32api.GetLastError() if not ok else 0
        except Exception:
            self._registered = False
            self.last_error = -1
        return self._registered

    def unregister(self) -> None:
        if self._registered:
            try:
                win32gui.UnregisterHotKey(self.hwnd, HOTKEY_ID)
            except Exception:
                pass
            self._registered = False

    def poll(self) -> None:
        if not self._registered:
            return
        msg = wintypes.MSG()
        while _user32.PeekMessageW(ctypes.byref(msg), 0, WM_HOTKEY, WM_HOTKEY, 1):
            if msg.message == WM_HOTKEY:
                try:
                    self.callback()
                except Exception:
                    pass

    def start(self) -> bool:
        return self.register()

    def stop(self) -> None:
        self.unregister()

    def update(self, vk: int, modifiers: int) -> bool:
        self.unregister()
        self.vk = vk
        self.modifiers = modifiers
        return self.register()


class ThreadHotkey:
    """バックグラウンドスレッド + メッセージ専用ウィンドウ。"""

    def __init__(self, vk: int, modifiers: int, callback):
        self.vk = vk
        self.modifiers = modifiers
        self.callback = callback
        self._ready = threading.Event()
        self._registered = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._wnd_proc_ref = None
        self.last_error: int = 0
        self._backend = "thread"

    @property
    def is_registered(self) -> bool:
        return self._registered

    def poll(self) -> None:
        pass

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            self.stop()
        self._stop.clear()
        self._ready.clear()
        self._registered = False
        self._thread = threading.Thread(
            target=self._run, name="InstantClipHotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self._registered

    def stop(self) -> None:
        self._stop.set()
        if self._hwnd:
            try:
                _user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._hwnd = None
        self._registered = False

    def update(self, vk: int, modifiers: int) -> bool:
        self.vk = vk
        self.modifiers = modifiers
        return self.start()

    def _run(self) -> None:
        hwnd = 0
        try:
            service = self

            def wnd_proc(hwnd, msg, wparam, lparam):
                if msg == WM_HOTKEY:
                    try:
                        service.callback()
                    except Exception:
                        pass
                    return 0
                if msg == WM_DESTROY:
                    _user32.PostQuitMessage(0)
                    return 0
                return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wnd_proc_ref = WNDPROC(wnd_proc)
            class_name = f"InstantClipHotkeyWnd_{id(self)}"

            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wnd_proc_ref
            wc.lpszClassName = class_name
            wc.hInstance = _kernel32.GetModuleHandleW(None)
            _user32.RegisterClassW(ctypes.byref(wc))

            hwnd = _user32.CreateWindowExW(
                0,
                class_name,
                "InstantClipHotkey",
                0,
                0,
                0,
                0,
                0,
                HWND_MESSAGE,
                None,
                wc.hInstance,
                None,
            )
            if not hwnd:
                self.last_error = _kernel32.GetLastError()
                return

            flags = (self.modifiers or 0) | MOD_NOREPEAT
            ok = _user32.RegisterHotKey(hwnd, HOTKEY_ID, flags, self.vk)
            if not ok:
                self.last_error = _kernel32.GetLastError()
                return

            self._hwnd = hwnd
            self._registered = True
        except Exception:
            self.last_error = -3
            self._registered = False

        self._ready.set()

        if not self._registered:
            if hwnd:
                try:
                    _user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            return

        try:
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if self._stop.is_set():
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            try:
                _user32.UnregisterHotKey(hwnd, HOTKEY_ID)
            except Exception:
                pass
            try:
                _user32.DestroyWindow(hwnd)
            except Exception:
                pass
            self._hwnd = None
            self._registered = False


def _error_label(err: int) -> str:
    if err == -2:
        return "keyboard パッケージ未インストール"
    if err == -3:
        return "スレッド内例外"
    if err == ERROR_HOTKEY_ALREADY_REGISTERED:
        return "他アプリが使用中"
    if err == 1407:
        return "HWND 無効"
    if err == 0:
        return "登録拒否"
    return f"code={err}"


def _hotkey_attempts(vk: int, modifiers: int) -> list[tuple[int, int, str]]:
    """登録を試すキー組み合わせ。指定キーを最優先し、登録できない時だけ代替へ進む。"""
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, str]] = []

    def add(v: int, m: int, label: str) -> None:
        key = (v, m)
        if key not in seen:
            seen.add(key)
            out.append((v, m, label))

    add(vk, modifiers, "指定キー")
    if not modifiers:
        add(vk, win32con.MOD_CONTROL, "Ctrl 付き")
    add(0x79, win32con.MOD_CONTROL, "Ctrl+F10")
    return out


def try_register_hotkey(
    hwnd_candidates: list[int],
    vk: int,
    modifiers: int,
    callback,
) -> tuple[DualHotkey | KeyboardHotkey | TkHotkey | ThreadHotkey | None, str]:
    """DualHotkey（thread、不可なら keyboard）→ Tk HWND の順で試す。"""
    errors: list[str] = []

    for v, m, label in _hotkey_attempts(vk, modifiers):
        dual = DualHotkey(v, m, callback)
        if dual.start():
            return dual, ""

        errors.append(
            f"{label}(dual): thread={_error_label(dual._thread.last_error)} "
            f"keyboard={_error_label(dual._keyboard.last_error)}"
        )
        dual.stop()

        for hwnd in hwnd_candidates:
            tk_hk = TkHotkey(hwnd, v, m, callback)
            if tk_hk.register():
                return tk_hk, ""
            errors.append(f"{label}(hwnd={hwnd}): {_error_label(tk_hk.last_error)}")
            tk_hk.unregister()

    return None, " / ".join(errors[:4])


# Shift / Ctrl / Alt / Win / CapsLock 自体（単独ではホットキーにしない）
MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})

_VK_NAMES = {
    0x13: "Pause",
    0x20: "Space",
    0x21: "PageUp",
    0x22: "PageDown",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2C: "PrintScreen",
    0x2D: "Insert",
    0x2E: "Delete",
    0x91: "ScrollLock",
}

# 修飾キーなしでも普段の入力を邪魔しにくいキー
_STANDALONE_VKS = frozenset({0x13, 0x2C, 0x91})


def vk_name(vk: int) -> str:
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"Num{vk - 0x60}"
    return _VK_NAMES.get(vk, f"VK={vk}")


def is_standalone_key(vk: int) -> bool:
    """修飾キーなしでホットキーにしてよいキーか（F キー・Pause など）。"""
    return 0x70 <= vk <= 0x87 or vk in _STANDALONE_VKS


def combo_label(vk: int, mod: int) -> str:
    return f"{mod_label(mod)}+{vk_name(vk)}".strip("+")


def mod_label(mod: int) -> str:
    parts = []
    if mod & win32con.MOD_CONTROL:
        parts.append("Ctrl")
    if mod & win32con.MOD_ALT:
        parts.append("Alt")
    if mod & win32con.MOD_SHIFT:
        parts.append("Shift")
    return "+".join(parts) if parts else ""
