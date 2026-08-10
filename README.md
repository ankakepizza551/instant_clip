# InstantClip

ホットキー一発で「直前 N 秒」の画面を mp4 に保存する常駐ツールです。  
NVIDIA ShadowPlay の Instant Replay / OBS のリプレイバッファと同様の動作です。

## 機能

- **リプレイバッファ**: 常時、直近 N 秒（3〜120秒、設定可能）をメモリ／一時ファイルに保持
- **ホットキー保存**: デフォルト **F9** でクリップを `clip_YYYYMMDD_HHMMSS.mp4` として保存
- **キャプチャモード**
  - **自動**: 天則 (th123) 起動中はゲームウィンドウ、未起動時はフルスクリーン
  - **フルスクリーン**: 仮想デスクトップ全体
  - **ゲームウィンドウ**: th123.exe のクライアント領域
- **ゲーム音のみ**: VB-Cable 経由でゲーム音声だけを録音（システム音・通話音を混ぜない）
- **exe 配布**: PyInstaller でビルド可能

## 必要なもの

- Windows 10 / 11
- Python 3.11+（開発時）
- **ffmpeg.exe** → `ffmpeg/ffmpeg.exe` に配置
- ゲーム音のみを使う場合: [VB-Cable](https://vb-audio.com/Cable/) のインストール

### FFmpeg のセットアップ

1. https://github.com/BtbN/FFmpeg-Builds/releases から `ffmpeg-master-latest-win64-gpl.zip`
2. 解凍して `bin/ffmpeg.exe` を `instant_clip/ffmpeg/ffmpeg.exe` にコピー

`tensoku_rep_movier/ffmpeg/ffmpeg.exe` があればビルド時に自動コピーされます。

## インストール（開発）

```powershell
cd instant_clip
pip install -r requirements.txt
python main.py
```

## 使い方

1. 起動するとバックグラウンドでバッファが自動開始します
2. プレイ中に **F9**（または設定したキー）を押す
3. 出力フォルダに直前 N 秒のクリップが保存されます（デフォルト: `Videos/InstantClip`）

ウィンドウを閉じるとタスクバー通知領域に常駐します（トレイアイコンから復帰・終了）。

## ゲーム音のみの設定

### 方法 A: 自動ルーティング（推奨）

1. VB-Cable をインストール
2. 「ゲーム音のみ」「ゲーム起動時に VB-Cable へ自動切替」をオン
3. **ツール起動後に** 天則を起動 → ゲームが VB-Cable に出力をバインド
4. 録音は VB-Cable のループバックから取得（Discord 等は混ざらない）

### 方法 B: 手動

天則の音声設定で出力デバイスを **CABLE Input** に指定してください。

## ビルド（配布用 exe）

```powershell
.\build_exe.ps1
```

または:

```powershell
python build.py
```

出力: `dist/InstantClip/` フォルダを zip で配布

## 設定項目

| 項目 | 説明 | デフォルト |
|------|------|-----------|
| バッファ秒数 | 保持する過去の長さ | 10 |
| ホットキー | F9 / F10 / Ctrl+F9 等 | F9 |
| キャプチャ | auto / fullscreen / game_window | auto |
| 出力フォルダ | クリップ保存先 | Videos/InstantClip |
| FPS | 録画フレームレート | 30 |
| ゲーム音のみ | VB-Cable ループバック録音 | オン |
| 自動ルーティング | ゲーム起動検出で VB-Cable 切替 | オン |

設定は `config.json` に保存されます（exe と同じフォルダ）。

## 注意

- 常時キャプチャのため CPU/GPU に負荷がかかります（NVENC 利用時は軽減）
- 天則が管理者権限で動いている場合、本ツールも管理者で起動が必要です（ビルド exe は UAC 管理者昇格）
- gdigrab は一部フルスクリーン DirectX タイトルで黒画面になることがあります
- バッファ開始直後は秒数が足りず保存できない場合があります
