import argparse
from pathlib import Path

from PIL import Image

from common import BASE_DIR, clean


def load_logo(source):
    if not clean(source):
        return None
    path = Path(source)
    path = path if path.is_absolute() else BASE_DIR / path
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except (OSError, ValueError):
        return None


def prepare_assets(symbol, wordmark, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in [("symbol", symbol), ("wordmark", wordmark)]:
        with Image.open(source) as image:
            rgba = image.convert("RGBA")
            bounds = rgba.getchannel("A").getbbox()
            if not bounds:
                raise ValueError(f"The {name} logo is empty.")
            cropped = rgba.crop(bounds)
            output = destination / f"uh-{name}.png"
            if output.exists():
                raise FileExistsError(f"Asset already exists: {output}")
            cropped.save(output, "PNG")
            print(f"{name}: original {image.size}, trimmed {cropped.size}, alpha {rgba.getchannel('A').getextrema()}")
            if name == "symbol":
                icon = Image.new("RGBA", (64, 64))
                cropped.thumbnail((48, 48), Image.Resampling.LANCZOS)
                icon.alpha_composite(cropped, ((64 - cropped.width) // 2, (64 - cropped.height) // 2))
                icon.save(destination / "uh-icon.png", "PNG")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare supplied UH Homes logos without changing their colors or proportions.")
    parser.add_argument("--symbol", type=Path, required=True)
    parser.add_argument("--wordmark", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=BASE_DIR / "brand_assets")
    args = parser.parse_args()
    prepare_assets(args.symbol, args.wordmark, args.output)
