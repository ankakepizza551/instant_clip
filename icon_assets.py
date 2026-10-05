"""InstantClip アプリアイコン（録画レコーダー風）。"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
ICON_PNG = HERE / "icon.png"
ICON_ICO = HERE / "icon.ico"

BG = (14, 17, 23)
CARD = (22, 27, 34)
ACCENT = (255, 71, 87)
ACCENT_GLOW = (255, 71, 87, 80)
FRAME = (0, 212, 170)


def create_app_icon(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    m = size // 10
    r_outer = size // 2 - m

    # 外枠（角丸カード）
    draw.rounded_rectangle(
        [m, m, size - m, size - m],
        radius=size // 5,
        fill=BG,
    )

    # 録画フレーム（四隅ブラケット）
    bracket = size // 5
    thick = max(2, size // 32)
    inset = size // 4
    for (x0, y0, x1, y1) in (
        (inset, inset, inset + bracket, inset + thick),
        (inset, inset, inset + thick, inset + bracket),
        (size - inset - bracket, inset, size - inset, inset + thick),
        (size - inset - thick, inset, size - inset, inset + bracket),
        (inset, size - inset - thick, inset + bracket, size - inset),
        (inset, size - inset - bracket, inset + thick, size - inset),
        (size - inset - bracket, size - inset - thick, size - inset, size - inset),
        (size - inset - thick, size - inset - bracket, size - inset, size - inset),
    ):
        draw.rectangle([x0, y0, x1, y1], fill=FRAME)

    cx, cy = size // 2, size // 2
    glow_r = size // 4
    draw.ellipse(
        [cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r],
        fill=ACCENT_GLOW,
    )
    dot_r = size // 7
    draw.ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
        fill=ACCENT,
    )
    hole = dot_r // 2
    draw.ellipse(
        [cx - hole, cy - hole, cx + hole, cy + hole],
        fill=CARD,
    )

    return img


def save_icons() -> None:
    base = create_app_icon(256)
    base.save(ICON_PNG, format="PNG")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    # ICO は元画像より大きいサイズを含められないので、最大サイズの画像から書き出す
    base.save(ICON_ICO, format="ICO", sizes=sizes)
    print(f"Generated: {ICON_ICO}, {ICON_PNG}")


def load_icon(size: int = 64) -> Image.Image:
    if ICON_PNG.exists():
        img = Image.open(ICON_PNG).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size), Image.Resampling.LANCZOS)
        return img
    return create_app_icon(size)


if __name__ == "__main__":
    save_icons()
