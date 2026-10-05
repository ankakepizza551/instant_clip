"""Windows ログオン時の自動起動（タスクスケジューラ）。

管理者権限で動く exe はスタートアップ／Run キーからは起動されないため、
「最上位の特権で実行」するログオンタスクとして登録する。
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

import win32api
import win32con

from ffmpeg_util import CREATE_NO_WINDOW

TASK_NAME = "InstantClip"
TRAY_ARG = "--tray"

# 既定値のままだと「バッテリー駆動では起動しない」「72 時間で強制終了」になるため XML で指定する
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _launch_command() -> tuple[str, str, str]:
    """(実行ファイル, 引数, 作業フォルダ)。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return str(exe), TRAY_ARG, str(exe.parent)
    here = Path(__file__).parent
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return str(exe), f'"{here / "main.py"}" {TRAY_ARG}', str(here)


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=20,
    )


def _error_text(result: subprocess.CompletedProcess) -> str:
    lines = [l.strip() for l in (result.stderr or result.stdout).splitlines() if l.strip()]
    return lines[-1] if lines else f"schtasks 終了コード {result.returncode}"


def is_enabled(task_name: str = TASK_NAME) -> bool:
    try:
        return _schtasks("/Query", "/TN", task_name).returncode == 0
    except Exception:
        return False


def set_enabled(enabled: bool, task_name: str = TASK_NAME) -> tuple[bool, str]:
    """自動起動を登録／解除する。(成功したか, エラー文) を返す。"""
    try:
        if not enabled:
            result = _schtasks("/Delete", "/TN", task_name, "/F")
            return result.returncode == 0, _error_text(result) if result.returncode else ""

        command, arguments, workdir = _launch_command()
        xml = _TASK_XML.format(
            user=escape(win32api.GetUserNameEx(win32con.NameSamCompatible)),
            command=escape(command),
            arguments=escape(arguments),
            workdir=escape(workdir),
        )
        with tempfile.TemporaryDirectory(prefix="instant_clip_task_") as tmp:
            xml_path = Path(tmp) / "task.xml"
            xml_path.write_text(xml, encoding="utf-16")
            result = _schtasks("/Create", "/TN", task_name, "/XML", str(xml_path), "/F")
        return result.returncode == 0, _error_text(result) if result.returncode else ""
    except Exception as e:
        return False, str(e)
