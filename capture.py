"""画面キャプチャ領域の計算とゲームウィンドウ検出。"""

from dataclasses import dataclass
import ctypes
from pathlib import Path
from typing import Callable

import win32gui
import win32process


@dataclass(frozen=True)
class CaptureTarget:
    """映像入力指定。gfxcapture は WGC ウィンドウ直接、gdigrab は画面領域。"""

    grabber: str = "gdigrab"  # "gfxcapture" | "gdigrab"
    mode: str = "region"  # "hwnd" | "window_exe" | "region"
    hwnd: int | None = None
    left: int = 0
    top: int = 0
    width: int = 1920
    height: int = 1080
    label: str = ""
    game_exe: str = ""


def _get_exe_name_for_pid(pid: int) -> str | None:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    hproc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size))
        return buf.value if ok else None
    finally:
        ctypes.windll.kernel32.CloseHandle(hproc)


def find_game_hwnd(exe_name: str, preferred_hwnd: int | None = None) -> int | None:
    """exe に一致するウィンドウのうち、最適なものを返す。"""
    if preferred_hwnd and hwnd_matches_game(preferred_hwnd, exe_name):
        if get_window_client_region(preferred_hwnd):
            return preferred_hwnd

    exe_lower = exe_name.lower()
    candidates: list[tuple[int, int]] = []
    foreground = win32gui.GetForegroundWindow()

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            path = _get_exe_name_for_pid(pid)
            if path and Path(path).name.lower() == exe_lower:
                region = get_window_client_region(hwnd)
                if not region:
                    return True
                left, top, w, h = region
                area = w * h
                score = area
                if hwnd == foreground:
                    score += 10_000_000
                if win32gui.GetWindowText(hwnd).strip():
                    score += 1000
                # (0,0) は別ウィンドウと誤認しやすいので優先度を下げる
                if left == 0 and top == 0:
                    score -= 500_000
                candidates.append((hwnd, score))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


def is_game_running(exe_name: str) -> bool:
    return find_game_hwnd(exe_name) is not None


def get_fullscreen_region() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    width = width if width % 2 == 0 else width - 1
    height = height if height % 2 == 0 else height - 1
    return left, top, width, height


def get_virtual_screen_bounds() -> tuple[int, int, int, int]:
    """仮想デスクトップ全体 (left, top, width, height)。"""
    return get_fullscreen_region()


def is_valid_gdigrab_region(left: int, top: int, width: int, height: int) -> bool:
    if width < 32 or height < 32:
        return False
    vl, vt, vw, vh = get_virtual_screen_bounds()
    if left < vl or top < vt:
        return False
    if left + width > vl + vw or top + height > vt + vh:
        return False
    return True


def normalize_gdigrab_region(
    left: int, top: int, width: int, height: int,
) -> tuple[int, int, int, int] | None:
    """gdigrab 用に仮想画面内へ収める。収められない場合は None。"""
    if width < 32 or height < 32:
        return None
    vl, vt, vw, vh = get_virtual_screen_bounds()
    vr, vb = vl + vw, vt + vh
    if left < vl:
        left = vl
    if top < vt:
        top = vt
    if left + width > vr:
        left = vr - width
    if top + height > vb:
        top = vb - height
    if not is_valid_gdigrab_region(left, top, width, height):
        return None
    return left, top, width, height


def hwnd_matches_game(hwnd: int, exe_name: str) -> bool:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        path = _get_exe_name_for_pid(pid)
        return path and Path(path).name.lower() == exe_name.lower()
    except Exception:
        return False


def get_window_client_region(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        if not win32gui.IsWindowVisible(hwnd):
            return None
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        pt = ctypes.wintypes.POINT(cl, ct)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        left, top = pt.x, pt.y
        width, height = cr - cl, cb - ct
        if width < 32 or height < 32:
            return None
        width = width if width % 2 == 0 else width - 1
        height = height if height % 2 == 0 else height - 1
        normalized = normalize_gdigrab_region(left, top, width, height)
        return normalized
    except Exception:
        return None


def get_window_client_size(hwnd: int) -> tuple[int, int] | None:
    try:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        width, height = cr - cl, cb - ct
        if width < 32 or height < 32:
            return None
        width = width if width % 2 == 0 else width - 1
        height = height if height % 2 == 0 else height - 1
        return width, height
    except Exception:
        return None


def _game_window_target(
    hwnd: int,
    title: str,
    game_exe: str,
    grabber: str,
    mode: str,
) -> CaptureTarget | None:
    size = get_window_client_size(hwnd)
    region = get_window_client_region(hwnd)
    if not size:
        return None
    w, h = size
    left, top = (region[0], region[1]) if region else (0, 0)
    rw = region[2] if region else w
    rh = region[3] if region else h
    use_region = mode == "region"
    return CaptureTarget(
        grabber=grabber,
        mode=mode,
        hwnd=hwnd,
        left=left,
        top=top,
        width=rw if use_region else w,
        height=rh if use_region else h,
        label=title,
        game_exe=game_exe,
    )


def resolve_capture_target(
    mode: str,
    game_exe: str,
    log: Callable[[str], None] = print,
    gfxcapture_available: bool = True,
) -> CaptureTarget:
    hwnd = find_game_hwnd(game_exe)
    use_window = mode == "game_window" or (mode == "auto" and hwnd)

    if use_window and hwnd and gfxcapture_available:
        title = win32gui.GetWindowText(hwnd)
        target = _game_window_target(hwnd, title, game_exe, "gdigrab", "region")
        if target:
            log(
                f"キャプチャ: ゲーム領域「{title}」 {target.width}x{target.height} "
                f"({target.left},{target.top})"
            )
            return target
        log("[警告] ゲームウィンドウ領域を取得できません。フルスクリーンにフォールバック")
    elif use_window and hwnd and not gfxcapture_available:
        title = win32gui.GetWindowText(hwnd)
        target = _game_window_target(hwnd, title, game_exe, "gdigrab", "region")
        if target:
            log(
                f"キャプチャ: ゲーム領域「{title}」 {target.width}x{target.height} "
                f"(重なりに注意)"
            )
            return target

    left, top, width, height = get_fullscreen_region()
    log(
        f"キャプチャ: フルスクリーン {width}x{height} "
        f"(画面上の重なりウィンドウも映ります)"
    )
    return CaptureTarget(
        grabber="gdigrab",
        mode="region",
        left=left,
        top=top,
        width=width,
        height=height,
        label="fullscreen",
        game_exe=game_exe,
    )


def build_capture_fallbacks(
    mode: str,
    game_exe: str,
    gfxcapture_available: bool = True,
) -> list[CaptureTarget]:
    """起動時に試すキャプチャ方式の順序（成功した方式をロックする）。"""
    hwnd = find_game_hwnd(game_exe)
    use_window = mode == "game_window" or (mode == "auto" and hwnd)
    out: list[CaptureTarget] = []

    if use_window and hwnd:
        title = win32gui.GetWindowText(hwnd)
        t = _game_window_target(hwnd, title, game_exe, "gdigrab", "region")
        if t:
            out.append(t)

    left, top, width, height = get_fullscreen_region()
    out.append(
        CaptureTarget(
            grabber="gdigrab",
            mode="region",
            left=left,
            top=top,
            width=width,
            height=height,
            label="fullscreen",
            game_exe=game_exe,
        )
    )
    return out


def refresh_capture_target(
    current: CaptureTarget,
    capture_mode: str,
    game_exe: str,
) -> CaptureTarget | None:
    """監視ループ用: 同じ grabber/mode のまま座標・HWND だけ更新する。"""
    hwnd = find_game_hwnd(game_exe, current.hwnd)
    use_window = capture_mode == "game_window" or (capture_mode == "auto" and hwnd)

    if current.mode in ("hwnd", "window_exe") and use_window and hwnd:
        title = win32gui.GetWindowText(hwnd)
        return _game_window_target(
            hwnd, title, game_exe, current.grabber, current.mode
        )

    if current.mode == "region" and current.label == "fullscreen":
        left, top, width, height = get_fullscreen_region()
        return CaptureTarget(
            grabber=current.grabber,
            mode="region",
            left=left,
            top=top,
            width=width,
            height=height,
            label="fullscreen",
            game_exe=game_exe,
        )

    if current.mode == "region" and use_window and hwnd:
        title = win32gui.GetWindowText(hwnd)
        return _game_window_target(
            hwnd, title, game_exe, current.grabber, "region"
        )

    return None


def target_needs_restart(
    old: CaptureTarget | None,
    new: CaptureTarget,
    position_threshold: int = 12,
) -> bool:
    if old is None:
        return True
    if (old.grabber, old.mode) != (new.grabber, new.mode):
        return True
    if new.mode == "hwnd":
        return old.hwnd != new.hwnd or (old.width, old.height) != (new.width, new.height)
    if new.mode == "window_exe":
        return (old.width, old.height) != (new.width, new.height)
    if (old.width, old.height) != (new.width, new.height):
        return True
    if (
        abs(old.left - new.left) > position_threshold
        or abs(old.top - new.top) > position_threshold
    ):
        return True
    return False


# 後方互換
def resolve_capture_region(
    mode: str,
    game_exe: str,
    log: Callable[[str], None] = print,
) -> tuple[int, int, int, int]:
    t = resolve_capture_target(mode, game_exe, log=log)
    return t.left, t.top, t.width, t.height
