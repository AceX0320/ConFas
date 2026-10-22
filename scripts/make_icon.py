"""Generate assets/confas.ico (pure Pillow, no external assets needed)."""
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    raise SystemExit("pip install Pillow first")


def make(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 16
    r = size // 6
    d.rounded_rectangle([m, m, size - m, size - m], radius=r, fill=(37, 99, 235, 255))
    # document glyph
    x0, y0, x1, y1 = size * 0.28, size * 0.20, size * 0.62, size * 0.80
    fold = size * 0.14
    d.polygon([(x0, y0), (x1, y0), (x1 + fold if False else x1, y0), (x1, y1), (x0, y1)],
              fill=(255, 255, 255, 255))
    d.polygon([(x1 - fold, y0), (x1, y0 + fold * 0), (x1, y0)], fill=(210, 225, 250, 255))
    # arrow (conversion)
    ax = size * 0.62
    d.line([(ax, size * 0.42), (size * 0.86, size * 0.42)], fill=(255, 214, 10, 255), width=max(2, size // 14))
    d.polygon([(size * 0.86, size * 0.34), (size * 0.94, size * 0.42), (size * 0.86, size * 0.50)],
              fill=(255, 214, 10, 255))
    return img


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "assets"
    out.mkdir(exist_ok=True)
    base = make(256)
    base.save(out / "confas.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", out / "confas.ico")
