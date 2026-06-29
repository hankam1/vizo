"""Generate logo.ico (Windows icon) from PNG. Run after make_logo.py."""
from PIL import Image

SIZES = [16, 32, 48, 64, 128, 256]

def main():
    # Use the largest PNG as source
    src = "logo_256.png"
    import os
    if not os.path.exists(src):
        print(f"Сначала запусти make_logo.py — нужен {src}")
        return
    img = Image.open(src).convert("RGBA")
    img.save("logo.ico", format="ICO", sizes=[(s, s) for s in SIZES])
    print("logo.ico создан")

if __name__ == "__main__":
    main()
