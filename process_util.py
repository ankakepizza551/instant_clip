"""プロセス終了ユーティリティ。"""

import subprocess

from ffmpeg_util import CREATE_NO_WINDOW


def kill_process_tree(pid: int) -> None:
    """Windows: 子プロセスごと強制終了。"""
    if pid <= 0:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        )
    except Exception:
        pass
