"""PWA用のアイコン画像を書き出す。

アイコンはA案(ブルー角丸): 青のグラデーション背景に、白い温度計とオレンジの液柱。

実行にはPillowが必要(アプリの実行時には不要なのでrequirements.txtには入れない):
    pip install Pillow
    python make_icons.py
"""

from PIL import Image, ImageDraw
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "static", "icons")

SS = 4  # 一度大きく描いてから縮小し、輪郭を滑らかにする倍率
CANVAS = 512  # 設計上の基準サイズ

BG_FROM = (0x2F, 0x77, 0xB8)
BG_TO = (0x19, 0x4A, 0x78)
WHITE = (255, 255, 255, 255)
ORANGE = (0xEE, 0x7D, 0x2D, 255)


def diagonal_gradient(size, color_from, color_to):
    """左上から右下へ向かう線形グラデーションを作る"""
    small = Image.new("RGB", (256, 256))
    pixels = small.load()
    for y in range(256):
        for x in range(256):
            t = (x + y) / 510
            pixels[x, y] = tuple(
                round(a + (b - a) * t) for a, b in zip(color_from, color_to)
            )
    return small.resize((size, size), Image.BILINEAR)


def draw_icon(size, maskable=False):
    """アイコンを1枚描く。

    maskable=True のときは角を丸めず全面を塗る。OS側が好きな形に切り抜くため、
    こちらで角を丸めると二重に丸まって縁が欠けて見える。
    """
    work = size * SS
    scale = work / CANVAS

    def s(value):
        return value * scale

    icon = Image.new("RGBA", (work, work), (0, 0, 0, 0))

    # 背景: グラデーションを角丸(または全面)の形で切り抜いて貼る
    shape = Image.new("L", (work, work), 0)
    shape_draw = ImageDraw.Draw(shape)
    if maskable:
        shape_draw.rectangle([0, 0, work, work], fill=255)
    else:
        shape_draw.rounded_rectangle([0, 0, work - 1, work - 1], radius=s(114), fill=255)
    icon.paste(diagonal_gradient(work, BG_FROM, BG_TO), (0, 0), shape)

    draw = ImageDraw.Draw(icon)

    # 温度計の本体(白)
    draw.rounded_rectangle([s(212), s(96), s(300), s(368)], radius=s(44), fill=WHITE)
    draw.ellipse([s(178), s(298), s(334), s(454)], fill=WHITE)

    # 液柱(オレンジ)
    draw.rounded_rectangle([s(234), s(168), s(278), s(380)], radius=s(22), fill=ORANGE)
    draw.ellipse([s(202), s(322), s(310), s(430)], fill=ORANGE)

    return icon.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    targets = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        # Androidが丸などに切り抜く用。図柄は中央80%に収まっているので欠けない
        ("icon-maskable-512.png", 512, True),
        # iOSは自前で角を丸めるので、こちらは角丸なしを渡す
        ("apple-touch-icon.png", 180, True),
        ("favicon-32.png", 32, False),
    ]

    for name, size, maskable in targets:
        path = os.path.join(OUT_DIR, name)
        draw_icon(size, maskable).save(path)
        print(f"{name:26} {size:>4}px  {os.path.getsize(path):>7,} bytes")

    # 古いブラウザ向けにfavicon.icoも用意する
    ico_path = os.path.join(OUT_DIR, "favicon.ico")
    draw_icon(64).save(ico_path, sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"{'favicon.ico':26} {'ico':>4}    {os.path.getsize(ico_path):>7,} bytes")


if __name__ == "__main__":
    main()
