"""Generate TimeTrackr.icns from the same clock art used for the tray icon.

Renders the clock (grey / idle variant) at every size macOS wants for an app
icon, supersampled for smooth edges, then packs them with `iconutil`.

Usage: python appicon.py <output.icns>
"""
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

# macOS .iconset members: (filename, pixel size)
ICONSET = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def draw_clock(px):
    """Draw the idle clock face at `px` pixels, supersampled 4x for smoothness."""
    ss = px * 4
    scale = ss / 64.0
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill, border = "#9E9E9E", "#424242"

    def s(v):
        return v * scale

    def w(v):
        return max(1, int(round(v * scale)))

    d.ellipse([s(2), s(2), s(62), s(62)], fill=fill, outline=border, width=w(3))
    d.ellipse([s(12), s(12), s(52), s(52)], fill="white", outline=border, width=w(2))
    d.line([s(32), s(32), s(32), s(18)], fill=border, width=w(3))
    d.line([s(32), s(32), s(43), s(38)], fill=border, width=w(2))
    d.ellipse([s(29), s(29), s(35), s(35)], fill=border)
    return img.resize((px, px), Image.LANCZOS)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python appicon.py <output.icns>")
    out = os.path.abspath(sys.argv[1])
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "TimeTrackr.iconset")
        os.makedirs(iconset)
        for name, size in ICONSET:
            draw_clock(size).save(os.path.join(iconset, name), "PNG")
        subprocess.run(["iconutil", "-c", "icns", "-o", out, iconset], check=True)
    print("wrote", out)


if __name__ == "__main__":
    main()
