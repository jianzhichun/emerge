#!/usr/bin/env python3
"""Generate emerge icon PNGs from path data using Pillow.

Usage:
    python3 scripts/generate_icons.py           # generate all PNGs into assets/
    python3 scripts/generate_icons.py --verify  # verify all files exist and are loadable
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ASSETS = Path(__file__).parent.parent / "assets"

# --- geometry helpers -------------------------------------------------------

def _cubic_bezier_points(p0, p1, p2, p3, steps: int = 40) -> list[tuple[float, float]]:
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3*p0[0] + 3*u**2*t*p1[0] + 3*u*t**2*p2[0] + t**3*p3[0]
        y = u**3*p0[1] + 3*u**2*t*p1[1] + 3*u*t**2*p2[1] + t**3*p3[1]
        pts.append((x, y))
    return pts


def _loop_polygon(cx: float, tip_x: float, tip_y: float,
                  bot_x: float, bot_y: float, size: float) -> list[tuple[float, float]]:
    """Return polygon points for one ∞ lobe.

    The lobe is a closed cubic bezier shape matching the SVG spec:
      M cx,cy  C cx,cy  x1,y1  tx,ty  C tx,ty  x2,y2  bx,by  C bx,by  ... Z

    We parameterize directly from the SVG path data rather than parsing it.
    """
    # SVG left lobe (scaled to `size`-px coordinate space from 64-px spec):
    # M32 32  C 32 32  26 21  20 21  C 13 21  13 43  20 43  C 26 43  32 32  32 32
    # Right lobe mirrors: M32 32  C 32 32  38 21  44 21  C 51 21  51 43  44 43  C 38 43  32 32  32 32
    s = size / 64.0
    # Build from raw SVG coords then scale
    raw_pts: list[tuple[float, float]] = []
    if tip_x < cx:  # left lobe (blue)
        raw_pts += _cubic_bezier_points((32, 32), (32, 32), (26, 21), (20, 21))
        raw_pts += _cubic_bezier_points((20, 21), (13, 21), (13, 43), (20, 43))
        raw_pts += _cubic_bezier_points((20, 43), (26, 43), (32, 32), (32, 32))
    else:  # right lobe (purple)
        raw_pts += _cubic_bezier_points((32, 32), (32, 32), (38, 21), (44, 21))
        raw_pts += _cubic_bezier_points((44, 21), (51, 21), (51, 43), (44, 43))
        raw_pts += _cubic_bezier_points((44, 43), (38, 43), (32, 32), (32, 32))
    return [(x * s, y * s) for (x, y) in raw_pts]


# --- drawing ----------------------------------------------------------------

def _draw_icon(size: int, *, tray: bool = False) -> "Image.Image":
    from PIL import Image, ImageDraw

    s = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not tray:
        # background rounded rect
        rx = round(14 * s)
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=rx, fill=(15, 23, 42, 255))

        # subtle glow at center
        cx, cy, gr = round(32 * s), round(32 * s), round(8 * s)
        for r in range(gr, 0, -1):
            alpha = int(30 * (r / gr))
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(99, 102, 241, alpha))

    # stroke width (scaled from 4.5 @ 64px)
    stroke_w = max(1, round(4.5 * s))

    # left lobe — blue
    left_pts = _loop_polygon(32 * s, 20 * s, 21 * s, 20 * s, 43 * s, size)
    color_blue = (255, 255, 255) if tray else (96, 165, 250)
    _draw_thick_polygon(draw, left_pts, color_blue, stroke_w)

    # right lobe — purple
    right_pts = _loop_polygon(32 * s, 44 * s, 21 * s, 44 * s, 43 * s, size)
    color_purple = (255, 255, 255) if tray else (167, 139, 250)
    _draw_thick_polygon(draw, right_pts, color_purple, stroke_w)

    if not tray:
        # direction arrow: points="44,21 48,26 40,24" scaled
        arrow = [(round(x * s), round(y * s)) for (x, y) in [(44, 21), (48, 26), (40, 24)]]
        draw.polygon(arrow, fill=(196, 181, 253, 229))

    # center dot
    dot_r = max(1, round(3.5 * s))
    cx, cy = round(32 * s), round(32 * s)
    dot_color = (255, 255, 255, 242)
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=dot_color)

    return img


def _draw_thick_polygon(draw, pts: list[tuple[float, float]], color, width: int) -> None:
    """Draw a polygon outline by rendering line segments with width."""
    ipts = [(round(x), round(y)) for (x, y) in pts]
    for i in range(len(ipts) - 1):
        draw.line([ipts[i], ipts[i + 1]], fill=color + (255,) if len(color) == 3 else color, width=width)


# --- main -------------------------------------------------------------------

def generate_all() -> None:
    ASSETS.mkdir(exist_ok=True)

    for sz in (16, 32, 64, 128, 256):
        # Draw at 256 then downscale for crisp results at small sizes
        work_size = max(sz, 256)
        img = _draw_icon(work_size)
        if work_size != sz:
            from PIL import Image
            img = img.resize((sz, sz), Image.LANCZOS)
        out = ASSETS / f"icon-{sz}.png"
        img.save(out, "PNG")
        print(f"  wrote {out}")

    # tray: white monochrome, transparent background, 64px
    tray_img = _draw_icon(64, tray=True)
    out = ASSETS / "icon-tray.png"
    tray_img.save(out, "PNG")
    print(f"  wrote {out}")


def verify() -> bool:
    from PIL import Image
    ok = True
    expected = [f"icon-{sz}.png" for sz in (16, 32, 64, 128, 256)] + ["icon-tray.png"]
    for name in expected:
        p = ASSETS / name
        if not p.exists():
            print(f"MISSING {p}")
            ok = False
            continue
        try:
            with Image.open(p) as im:
                im.verify()
            print(f"  ok  {p}  ({p.stat().st_size} bytes)")
        except Exception as e:
            print(f"CORRUPT {p}: {e}")
            ok = False
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.verify:
        sys.exit(0 if verify() else 1)
    else:
        generate_all()
        print("done.")
