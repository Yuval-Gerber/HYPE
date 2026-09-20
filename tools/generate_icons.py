"""Generate clean monochrome (black) line icons for the sidebar + top bar.

Drawn with Pillow as black shapes on transparent backgrounds; the UI tints them
(slate when idle, ocean when active) at runtime. Output: hype/ui/assets/icons/.

  python tools/generate_icons.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

ICONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "hype", "ui", "assets", "icons")
S = 128                      # canvas
BLACK = (0, 0, 0, 255)
W = 10                       # stroke width


def _canvas():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def positions():
    img, d = _canvas()
    pts = [(18, 92), (46, 58), (72, 76), (104, 30)]
    d.line(pts, fill=BLACK, width=W, joint="curve")
    for x, y in pts:
        d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=BLACK)
    # arrow head at the end
    d.line([(104, 30), (88, 32)], fill=BLACK, width=W, joint="curve")
    d.line([(104, 30), (102, 48)], fill=BLACK, width=W, joint="curve")
    return img


def performance():
    img, d = _canvas()
    bars = [(24, 70), (54, 44), (84, 56)]
    for x, top in bars:
        d.rounded_rectangle([x, top, x + 22, 104], radius=5, fill=BLACK)
    return img


def traders():
    img, d = _canvas()
    # two overlapping user silhouettes
    def user(cx, hy, r, by):
        d.ellipse([cx - r, hy - r, cx + r, hy + r], fill=BLACK)         # head
        d.pieslice([cx - r * 1.7, by, cx + r * 1.7, by + r * 2.6], 180, 360, fill=BLACK)  # shoulders
    user(82, 46, 16, 70)
    user(50, 52, 18, 80)
    return img


def settings():
    img, d = _canvas()
    rows = [(34, 84), (64, 44), (94, 90)]   # (y, knob_x)
    for y, kx in rows:
        d.line([(20, y), (108, y)], fill=BLACK, width=W, joint="curve")
        d.ellipse([kx - 13, y - 13, kx + 13, y + 13], fill=BLACK)
    return img


def activity():
    img, d = _canvas()
    for y in (34, 64, 94):
        d.ellipse([20, y - 6, 32, y + 6], fill=BLACK)
        d.line([(46, y), (106, y)], fill=BLACK, width=W, joint="curve")
    return img


def system():
    img, d = _canvas()
    d.rounded_rectangle([18, 26, 110, 84], radius=10, outline=BLACK, width=W)
    d.line([(64, 84), (64, 98)], fill=BLACK, width=W)          # stand
    d.line([(44, 100), (84, 100)], fill=BLACK, width=W, joint="curve")  # base
    return img


def lock():
    img, d = _canvas()
    # shackle (open-bottom arc)
    d.arc([42, 24, 86, 76], 180, 360, fill=BLACK, width=W)
    d.line([(42, 50), (42, 64)], fill=BLACK, width=W)
    d.line([(86, 50), (86, 64)], fill=BLACK, width=W)
    # body
    d.rounded_rectangle([32, 60, 96, 106], radius=10, fill=BLACK)
    # keyhole
    d.ellipse([58, 74, 70, 86], fill=(255, 255, 255, 255))
    d.polygon([(61, 82), (67, 82), (69, 98), (59, 98)], fill=(255, 255, 255, 255))
    return img


def close_x():
    img, d = _canvas()
    d.line([(36, 36), (92, 92)], fill=BLACK, width=W, joint="curve")
    d.line([(92, 36), (36, 92)], fill=BLACK, width=W, joint="curve")
    return img


# Chevrons for spinbox arrows — baked in muted slate (QSS can't tint images).
MUTED = (100, 116, 139, 255)


def chevron_up():
    img, d = _canvas()
    d.line([(30, 80), (64, 44), (98, 80)], fill=MUTED, width=13, joint="curve")
    return img


def chevron_down():
    img, d = _canvas()
    d.line([(30, 48), (64, 84), (98, 48)], fill=MUTED, width=13, joint="curve")
    return img


def chevron_left():
    img, d = _canvas()
    d.line([(80, 26), (42, 64), (80, 102)], fill=MUTED, width=13, joint="curve")
    return img


def chevron_right():
    img, d = _canvas()
    d.line([(48, 26), (86, 64), (48, 102)], fill=MUTED, width=13, joint="curve")
    return img


ICON_FUNCS = {
    "nav_positions": positions,
    "nav_performance": performance,
    "nav_traders": traders,
    "nav_settings": settings,
    "nav_activity": activity,
    "nav_system": system,
    "lock": lock,
    "close": close_x,
    "chevron_up": chevron_up,
    "chevron_down": chevron_down,
    "chevron_left": chevron_left,
    "chevron_right": chevron_right,
}


def main() -> int:
    os.makedirs(ICONS, exist_ok=True)
    for name, fn in ICON_FUNCS.items():
        fn().save(os.path.join(ICONS, f"{name}.png"))
    print(f"wrote {len(ICON_FUNCS)} icons to {ICONS}")
    # contact sheet for review
    sheet = Image.new("RGBA", (S * len(ICON_FUNCS), S), (255, 255, 255, 255))
    for i, fn in enumerate(ICON_FUNCS.values()):
        sheet.paste(fn(), (i * S, 0), fn())
    sheet.save(os.path.join(ICONS, "_contact_sheet.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
