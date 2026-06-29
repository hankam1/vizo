"""Generate logo PNGs in multiple sizes. Run: pip install pillow && py make_logo.py"""
from PIL import Image, ImageDraw

SIZES = [64, 128, 256, 512, 1024]
COLOR = (204, 120, 92, 255)  # #CC785C
ASPECT = 64 / 56  # viewBox ratio

def make(size: int, transparent: bool = True):
    h = size
    w = int(size * ASPECT)
    bg = (0, 0, 0, 0) if transparent else (31, 29, 26, 255)
    img = Image.new("RGBA", (w, h), bg)
    draw = ImageDraw.Draw(img)

    s = h / 56
    # Chevron polygon: outer V + inner V cut-out
    points = [
        (int(8 * s), int(8 * s)),
        (int(32 * s), int(48 * s)),
        (int(56 * s), int(8 * s)),
        (int(44 * s), int(8 * s)),
        (int(32 * s), int(28 * s)),
        (int(20 * s), int(8 * s)),
    ]
    draw.polygon(points, fill=COLOR)

    suffix = "" if transparent else "_bg"
    img.save(f"logo_{size}{suffix}.png")
    print(f"  logo_{size}{suffix}.png ({w}x{h})")

if __name__ == "__main__":
    print("Generating logos...")
    for s in SIZES:
        make(s, transparent=True)
    make(256, transparent=False)
    print("Done.")
