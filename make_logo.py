"""Generate the vizo logo PNGs (the 6-spoke mark used in the app).
Run: pip install pillow && py make_logo.py"""
from PIL import Image, ImageDraw

SIZES = [64, 128, 256, 512, 1024]
COLOR = (111, 138, 255, 255)   # #6f8aff — app accent
DARK_BG = (13, 12, 18, 255)    # #0d0c12 — app background (for the _bg variant)

# Geometry taken from the in-app SVG (viewBox 280): one rounded bar
#   x=124 w=32  -> width 32/280 of the canvas
#   y=16  h=248 -> length 248/280
# drawn three times at 0/60/120 degrees about the center = a 6-spoke star.
BAR_W = 32 / 280
BAR_H = 248 / 280


def make(size: int, bg=None) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), bg or (0, 0, 0, 0))
    bw = size * BAR_W
    bh = size * BAR_H
    c = size / 2.0
    box = [c - bw / 2, (size - bh) / 2, c + bw / 2, (size + bh) / 2]
    for angle in (0, 60, 120):
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(box, radius=bw / 2, fill=COLOR)
        if angle:
            layer = layer.rotate(angle, resample=Image.BICUBIC, center=(c, c))
        canvas = Image.alpha_composite(canvas, layer)
    return canvas


if __name__ == "__main__":
    print("Generating vizo logos...")
    for s in SIZES:
        make(s).save(f"logo_{s}.png")
        print(f"  logo_{s}.png")
    make(256, bg=DARK_BG).save("logo_256_bg.png")
    print("  logo_256_bg.png")
    print("Done.")
