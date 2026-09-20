"""Generate the Hype app icon/logo: a thin ocean-blue triangle outline with the
word "Hype" inside it, on a transparent background. Produces a master PNG, a
logo PNG, and a macOS .icns.

  python tools/generate_icon.py

Outputs into hype/ui/assets/.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

OCEAN = (2, 119, 189, 255)        # C.OCEAN — same as the Hype text / borders
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hype", "ui", "assets")

# Cooler geometric fonts (pairs well with the triangle). First that loads wins.
FONT_CANDIDATES = [
    ("/System/Library/Fonts/Supplemental/Futura.ttc", 0),
    ("/System/Library/Fonts/Avenir Next.ttc", 0),
    ("/System/Library/Fonts/Avenir.ttc", 0),
    ("/System/Library/Fonts/HelveticaNeue.ttc", 0),
]


def _load_font(px: int):
    for path, idx in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, px, index=idx)
        except Exception:
            continue
    return ImageFont.load_default()


def draw_logo(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Upward triangle, symmetric, bold visible outline.
    m = size * 0.10
    apex = (size / 2, m)
    bl = (m, size - m)
    br = (size - m, size - m)
    line_w = max(3, int(size * 0.030))   # bulky, clearly visible

    d.line([apex, bl, br, apex], fill=OCEAN, width=line_w, joint="curve")

    # Center "Hype" on the triangle's centroid (its true middle).
    cx = (apex[0] + bl[0] + br[0]) / 3
    cy = (apex[1] + bl[1] + br[1]) / 3

    def tri_halfwidth(y: float) -> float:
        f = (y - apex[1]) / (bl[1] - apex[1])
        return max(0.0, f) * (br[0] - apex[0])

    text = "Hype"
    fs = int(size * 0.24)
    while fs > 10:
        font = _load_font(fs)
        stroke = max(1, int(fs * 0.03))   # thickens the glyphs (bulkier)
        box = d.textbbox((0, 0), text, font=font, stroke_width=stroke)
        tw, th = box[2] - box[0], box[3] - box[1]
        # Fit within the triangle width at the TOP of the text (narrowest band).
        if tw <= 2 * tri_halfwidth(cy - th / 2) * 0.84:
            break
        fs -= 4
    font = _load_font(fs)
    d.text((cx, cy), text, font=font, fill=OCEAN, anchor="mm",
           stroke_width=max(1, int(fs * 0.03)), stroke_fill=OCEAN)
    return img


def main() -> int:
    os.makedirs(ASSETS, exist_ok=True)
    master = draw_logo(1024)
    master.save(os.path.join(ASSETS, "icon_1024.png"))
    master.save(os.path.join(ASSETS, "logo.png"))
    print("wrote icon_1024.png + logo.png")

    iconset = os.path.join(ASSETS, "Hype.iconset")
    os.makedirs(iconset, exist_ok=True)
    specs = [(16, ""), (16, "@2x"), (32, ""), (32, "@2x"), (128, ""),
             (128, "@2x"), (256, ""), (256, "@2x"), (512, ""), (512, "@2x")]
    for base, suffix in specs:
        px = base * (2 if suffix else 1)
        master.resize((px, px), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{base}x{base}{suffix}.png"))

    icns_path = os.path.join(ASSETS, "Hype.icns")
    try:
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns_path], check=True)
        print(f"wrote {icns_path}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        master.save(icns_path)
        print(f"iconutil unavailable ({e}); wrote {icns_path} via Pillow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
