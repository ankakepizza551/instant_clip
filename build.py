"""配布用 exe をビルドする。 python build.py で実行。"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DIST = HERE / "dist" / "InstantClip"
NAME = "InstantClip"


def ensure_customtkinter_assets(dist_dir: Path) -> None:
    """PyInstaller で themes/fonts が欠ける場合の保険コピー。"""
    import customtkinter

    assets_src = Path(customtkinter.__file__).parent / "assets"
    if not assets_src.is_dir():
        print("[警告] customtkinter assets ソースが見つかりません")
        return

    assets_dst = dist_dir / "_internal" / "customtkinter" / "assets"
    assets_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(assets_src, assets_dst, dirs_exist_ok=True)

    theme = assets_dst / "themes" / "blue.json"
    if theme.exists():
        print("customtkinter assets を同梱しました")
    else:
        print("[警告] blue.json の同梱に失敗しました")


def run(cmd: list) -> None:
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print(f"\n[エラー] コマンド失敗: {' '.join(str(c) for c in cmd)}")
        sys.exit(1)


def main() -> None:
    print("===== InstantClip ビルド =====\n")

    icon_script = HERE / "icon_assets.py"
    if icon_script.exists():
        print("アイコンを生成しています...")
        run([sys.executable, str(icon_script)])

    req = HERE / "requirements.txt"
    if req.exists():
        print("依存パッケージをインストールしています...")
        run([sys.executable, "-m", "pip", "install", "-r", str(req)])

    if subprocess.run(
        [sys.executable, "-c", "import customtkinter"],
        capture_output=True,
    ).returncode != 0:
        print("[エラー] customtkinter のインストールに失敗しました")
        sys.exit(1)

    if subprocess.run(
        [sys.executable, "-m", "pip", "show", "pyinstaller"],
        capture_output=True,
    ).returncode != 0:
        print("PyInstaller をインストールしています...")
        run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    icon = HERE / "icon.ico"
    icon_args = [f"--icon={icon}"] if icon.exists() else []

    py_ver = f"{sys.version_info.major}{sys.version_info.minor}"
    dll_name = f"python{py_ver}.dll"
    dll_src = None
    for search_dir in [
        Path(sys.executable).parent,
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.executable).parent.parent,
    ]:
        candidate = search_dir / dll_name
        if candidate.exists():
            dll_src = candidate
            break
    dll_args = ["--add-binary", f"{dll_src};."] if dll_src else []

    # 前回ビルドの作業ディレクトリを消去（dist は実行中だと削除できないため PyInstaller に任せる）
    for stale in (HERE / "build", HERE / f"{NAME}.spec"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        elif stale.is_file():
            stale.unlink(missing_ok=True)

    print("※ InstantClip.exe が起動中の場合は終了してからビルドしてください\n")

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--windowed",
            "--uac-admin",
            f"--name={NAME}",
            *icon_args,
            *dll_args,
            # ウィンドウ・トレイのアイコン用（icon_assets が実行時に読む）
            "--add-data",
            "icon.ico;.",
            "--add-data",
            "icon.png;.",
            "--collect-all",
            "customtkinter",
            "--collect-data",
            "customtkinter",
            "--copy-metadata",
            "customtkinter",
            "--hidden-import",
            "customtkinter",
            "--hidden-import",
            "customtkinter.windows.widgets",
            "--hidden-import",
            "audio_routing",
            "--hidden-import",
            "file_acl",
            "--hidden-import",
            "autostart",
            "--hidden-import",
            "process_util",
            "--hidden-import",
            "keyboard",
            "--collect-all",
            "keyboard",
            "--hidden-import",
            "PIL",
            "--hidden-import",
            "PIL.Image",
            "--hidden-import",
            "PIL.ImageDraw",
            "--hidden-import",
            "pystray",
            "--collect-all",
            "pyaudiowpatch",
            "--manifest",
            "app.manifest",
            "main.py",
        ]
    )

    ffmpeg_src = HERE / "ffmpeg" / "ffmpeg.exe"
    ffmpeg_dst = DIST / "ffmpeg"
    if ffmpeg_src.exists():
        print("ffmpeg をコピーしています...")
        ffmpeg_dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ffmpeg_src, ffmpeg_dst / "ffmpeg.exe")
    else:
        tensoku_ffmpeg = HERE.parent / "tensoku_rep_movier" / "ffmpeg" / "ffmpeg.exe"
        if tensoku_ffmpeg.exists():
            print("tensoku_rep_movier の ffmpeg をコピーしています...")
            ffmpeg_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tensoku_ffmpeg, ffmpeg_dst / "ffmpeg.exe")
        else:
            print("[注意] ffmpeg\\ffmpeg.exe が見つかりません。手動でコピーしてください。")

    readme = HERE / "README.txt"
    if readme.exists():
        shutil.copy2(readme, DIST / "README.txt")

    if DIST.is_dir():
        ensure_customtkinter_assets(DIST)

    print(f"\n===== ビルド完了 =====")
    print(f"出力先: {DIST}")


if __name__ == "__main__":
    main()
