"""Rasterize the app logo into icon.ico, favicon.ico and logo.png.

The logo is simple geometry (rounded square, a Z, a T, a centre dot), so we
draw it directly with Pillow at high resolution and downscale for crisp,
antialiased output — no SVG renderer needed.

Run:  python gen_icon.py
Outputs:
  assets/icon.ico          (multi-size, used for POS.exe + the installer)
  static/img/favicon.ico   (browser tab icon)
  static/img/logo.png      (256px, used in the app's top bar)
"""
import os
from PIL import Image, ImageDraw, ImageFont

VIEW = 220          # SVG viewBox size
SS = 8              # supersample factor for antialiasing
BG = "#0B1220"
BLUE = "#2563EB"    # the Z
LIME = "#84CC16"    # the T


def _bold_font_path():
    """Heaviest available bold sans on this machine for the ZT wordmark."""
    win = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("ariblk.ttf", "segoeuib.ttf", "arialbd.ttf", "seguisb.ttf"):
        p = os.path.join(win, "Fonts", name)
        if os.path.exists(p):
            return p
    return None


def _fit_font(draw, text, target_width):
    """A bold font sized so `text` is about `target_width` wide."""
    path = _bold_font_path()
    if path is None:
        return ImageFont.load_default()
    probe = ImageFont.truetype(path, 100)
    w = draw.textlength(text, font=probe) or 1
    return ImageFont.truetype(path, max(8, int(100 * target_width / w)))


def render(size):
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded background square.
    radius = 48 * big / VIEW
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=BG)

    # Bold "ZT" wordmark, centred and filling ~72% of the width: Z in blue,
    # T in lime. Drawn as two letters so each gets its own colour.
    font = _fit_font(d, "ZT", big * 0.72)
    box = d.textbbox((0, 0), "ZT", font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = (big - tw) / 2 - box[0]
    y = (big - th) / 2 - box[1]
    d.text((x, y), "Z", font=font, fill=BLUE)
    d.text((x + d.textlength("Z", font=font), y), "T", font=font, fill=LIME)

    return img.resize((size, size), Image.LANCZOS)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(os.path.join(here, "assets"), exist_ok=True)
    os.makedirs(os.path.join(here, "static", "img"), exist_ok=True)

    master = render(256)
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = {s: render(s) for s in sizes}
    icon_images = [imgs[s] for s in sizes]

    ico_path = os.path.join(here, "assets", "icon.ico")
    master.save(ico_path, format="ICO",
                sizes=[(s, s) for s in sizes],
                append_images=icon_images)
    print("wrote", ico_path)

    fav_path = os.path.join(here, "static", "img", "favicon.ico")
    master.save(fav_path, format="ICO", sizes=[(32, 32), (16, 16)])
    print("wrote", fav_path)

    png_path = os.path.join(here, "static", "img", "logo.png")
    master.save(png_path, format="PNG")
    print("wrote", png_path)


if __name__ == "__main__":
    main()
