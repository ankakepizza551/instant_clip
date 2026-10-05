"""保存ファイルの ACL 調整（管理者起動時の閲覧不能を防ぐ）。"""

import os
import stat
from pathlib import Path


def ensure_user_can_access(path: Path) -> None:
    """現在ユーザーが読み書きできるよう DACL を調整する。"""
    try:
        import win32api
        import win32security
        import ntsecuritycon as con

        user, _, _ = win32security.LookupAccountName("", win32api.GetUserName())
        sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            dacl = win32security.ACL()
        wanted = con.FILE_GENERIC_READ | con.FILE_GENERIC_WRITE | con.DELETE
        # 既に同じ許可があれば追加しない（保存のたびに ACE が増えるのを防ぐ）
        for i in range(dacl.GetAceCount()):
            (ace_type, _flags), mask, sid = dacl.GetAce(i)[:3]
            if (
                ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE
                and sid == user
                and mask & wanted == wanted
            ):
                return
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, wanted, user)
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, sd)
    except Exception:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        except Exception:
            pass


def ensure_dir_user_can_access(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    ensure_user_can_access(path)
