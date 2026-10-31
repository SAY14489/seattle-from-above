"""Build contact-sheet grids of every site's before/after thumbnail pair, for
quick visual review (used to hand-classify change type -- see classify_sites.py).

Run: uv run python make_contact_sheets.py
"""

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WEB_DATA_DIR = Path(__file__).parent.parent / "web" / "public" / "data"
THUMB_DIR = Path(__file__).parent.parent / "web" / "public" / "thumbs"
OUT_DIR = Path(__file__).parent.parent / "data" / "contact_sheets"

CELL_IMG = 150  # each before/after thumbnail shown at this size
PER_SHEET = 10  # sites per sheet
COLS = 2


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(WEB_DATA_DIR / "sites.json") as f:
        sites = json.load(f)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
    except Exception:
        font = ImageFont.load_default()

    cell_w = CELL_IMG * 2 + 10
    cell_h = CELL_IMG + 34
    rows = (PER_SHEET + COLS - 1) // COLS

    for sheet_idx in range(0, len(sites), PER_SHEET):
        chunk = sites[sheet_idx : sheet_idx + PER_SHEET]
        sheet = Image.new("RGB", (cell_w * COLS + 10, cell_h * rows + 10), "white")
        draw = ImageDraw.Draw(sheet)

        for i, s in enumerate(chunk):
            col, row = i % COLS, i // COLS
            x0, y0 = 5 + col * cell_w, 5 + row * cell_h

            label = f"#{s['rank']} {s['id']}"
            draw.text((x0, y0), label, fill="black", font=font)

            for j, key in enumerate(["before_image", "after_image"]):
                img_path = THUMB_DIR / Path(s[key]).name if s.get(key) else None
                tx = x0 + j * (CELL_IMG + 10)
                ty = y0 + 18
                if img_path and img_path.exists():
                    im = Image.open(img_path).convert("RGB").resize((CELL_IMG, CELL_IMG))
                    sheet.paste(im, (tx, ty))
                else:
                    draw.rectangle([tx, ty, tx + CELL_IMG, ty + CELL_IMG], outline="red")

        out_path = OUT_DIR / f"sheet_{sheet_idx // PER_SHEET + 1:02d}.png"
        sheet.save(out_path)
        print("wrote", out_path)


if __name__ == "__main__":
    main()
