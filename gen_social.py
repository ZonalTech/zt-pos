"""Generate social-media images that reuse the app icon + the ZT POS wordmark.

Reuses gen_icon.render() so the mark is pixel-identical to icon.ico / logo.png,
places it on the brand-dark canvas with a soft accent glow, and adds the name.

Run:  python gen_social.py
Outputs:
  static/img/social.png          1200x630  (Open Graph / link-preview, Twitter/X, FB, LinkedIn)
  static/img/social-square.png   1080x1080 (Instagram / profile / WhatsApp)
"""
import os

from PIL import Image, ImageDraw, ImageFont, ImageFilter

import gen_icon  # reuse render() — same icon as icon.ico / favicon / logo.png

HERE = os.path.dirname(os.path.abspath(__file__))

# Brand palette (matches gen_icon.py).
BG = (11, 18, 32)        # #0B1220
BG2 = (13, 21, 38)       # slightly lighter for a subtle gradient
WHITE = (241, 245, 250)
GRAY = (148, 163, 184)   # #94A3B8
MUTED = (120, 140, 165)
CYAN = (34, 211, 238)    # #22D3EE
BLUE = (37, 99, 235)     # #2563EB

WINFONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def _font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(os.path.join(WINFONTS, n), size)
        except OSError:
            continue
    return ImageFont.load_default()


def bold(size):
    return _font(["segoeuib.ttf", "arialbd.ttf"], size)


def reg(size):
    return _font(["segoeui.ttf", "arial.ttf"], size)


def fit_bold(draw, text, max_w, start=150):
    """A bold font sized so `text` is about `max_w` wide (never larger than start)."""
    w = draw.textlength(text, font=bold(start)) or 1
    return bold(min(start, max(8, int(start * max_w / w))))


def _vgradient(w, h, top, bottom):
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / (h - 1)
        col.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t)
                                   for i in range(3)))
    return col.resize((w, h))


def _glow(size, center, radius, color, alpha):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=color + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(radius * 0.5))


def _place_icon(canvas, size, x, y):
    """Paste the app icon with a soft drop shadow + subtle border so its dark
    rounded square reads against the dark canvas."""
    radius = 48 * size / 220  # same corner radius gen_icon uses
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [x, y + 12, x + size, y + size + 12], radius=radius, fill=(0, 0, 0, 170))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(20)))
    canvas.alpha_composite(gen_icon.render(size), (x, y))
    ImageDraw.Draw(canvas).rounded_rectangle(
        [x, y, x + size - 1, y + size - 1], radius=radius,
        outline=(51, 65, 85, 255), width=2)


def make_og():
    W, H = 1200, 630
    canvas = _vgradient(W, H, BG, BG2).convert("RGBA")
    canvas.alpha_composite(_glow((W, H), (330, 315), 320, BLUE, 75))
    canvas.alpha_composite(_glow((W, H), (330, 315), 210, CYAN, 55))

    isz = 360
    _place_icon(canvas, isz, 150, (H - isz) // 2)

    d = ImageDraw.Draw(canvas)
    tx = 560
    icon_cy = (H - isz) // 2 + isz // 2  # vertical centre of the icon
    f = fit_bold(d, "Zonal Tech", W - tx - 70, start=104)
    box = d.textbbox((0, 0), "Zonal Tech", font=f)
    th = box[3] - box[1]
    ty = icon_cy - th // 2 - box[1] - 16
    d.text((tx, ty), "Zonal Tech", font=f, fill=WHITE)
    d.rounded_rectangle([tx, ty + th + 34, tx + 168, ty + th + 42],
                        radius=4, fill=CYAN)

    out = os.path.join(HERE, "static", "img", "social.png")
    canvas.convert("RGB").save(out)
    return out


def make_square():
    S = 1080
    canvas = _vgradient(S, S, BG, BG2).convert("RGBA")
    canvas.alpha_composite(_glow((S, S), (S // 2, 430), 440, BLUE, 75))
    canvas.alpha_composite(_glow((S, S), (S // 2, 430), 300, CYAN, 50))

    isz = 440
    _place_icon(canvas, isz, (S - isz) // 2, 220)

    d = ImageDraw.Draw(canvas)

    def centered(y, text, fnt, fill):
        w = d.textlength(text, font=fnt)
        d.text(((S - w) / 2, y), text, font=fnt, fill=fill)

    centered(770, "Zonal Tech", fit_bold(d, "Zonal Tech", S - 200, start=132),
             WHITE)

    out = os.path.join(HERE, "static", "img", "social-square.png")
    canvas.convert("RGB").save(out)
    return out


def main():
    os.makedirs(os.path.join(HERE, "static", "img"), exist_ok=True)
    for out in (make_og(), make_square()):
        print("wrote", out)


if __name__ == "__main__":
    main()
