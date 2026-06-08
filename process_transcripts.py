"""
process_transcripts.py
-----------------------
Batch-process a folder of PDF transcripts. Fully local — no network calls,
no Adobe Acrobat, no server.

For each PDF in the target folder:
  1. Extract the student name from a "Student Name:" label in the PDF text.
  2. Add a light-gray "Official" watermark (45° diagonal, centered near the
     bottom of each page) to every page.
  3. Save to <folder>/output/ as "Last, First, Transcript.pdf"

REQUIREMENTS
  pip install pymupdf

USAGE
  python process_transcripts.py <folder>
  python process_transcripts.py <folder> --dry-run
  python process_transcripts.py <folder> --overwrite
"""

import argparse
import platform
import re
import sys
from pathlib import Path

import fitz  # pymupdf

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WATERMARK_TEXT         = "Official"
WATERMARK_COLOR        = (0.83, 0.83, 0.83)  # light gray (RGB 0-1)
WATERMARK_ROTATION     = 45                   # degrees, counter-clockwise
WATERMARK_FILL_OPACITY = 1.0                  # 100 %
WATERMARK_SCALE        = 0.35                 # text width as fraction of page width
WATERMARK_HORIZ_OFFSET = -0.1 * 72           # pts: negative = left of page center
WATERMARK_VERT_OFFSET  =  3.5 * 72           # pts: positive = below page center

OUTPUT_DIRNAME = "output"

NAME_LABEL_PATTERN = re.compile(r"Student\s*Name\s*[:\-]\s*(.+)", re.IGNORECASE)
NAME_INLINE_PATTERN = re.compile(r"^[A-Za-z\-']+,\s*[A-Za-z]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_student_name(doc: fitz.Document) -> str | None:
    # Strategy 1: look for "Student Name: ..." label on any page
    for page in doc:
        for line in page.get_text().splitlines():
            m = NAME_LABEL_PATTERN.search(line)
            if m:
                name = m.group(1).strip()
                name = re.split(r"\s{2,}|\bDate\b|\bID\b", name)[0].strip()
                if name:
                    return name

    # Strategy 2: on page 1, the line immediately before "Crs-ID" is the
    # student name in "Last, First Middle" format (e.g. Palos Verdes transcripts)
    lines = [l.strip() for l in doc[0].get_text().splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line == "Crs-ID" and i > 0:
            candidate = lines[i - 1]
            if NAME_INLINE_PATTERN.match(candidate):
                return candidate

    return None


def to_last_first(full_name: str) -> str:
    # "Last, First [Middle]" → "Last, First"
    if "," in full_name:
        last, rest = full_name.split(",", 1)
        first = rest.strip().split()[0]
        return f"{last.strip()}, {first}"
    # "First [Middle] Last" → "Last, First"
    parts = full_name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}, {parts[0]}"


def safe_filename(stem: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", stem).strip()


def _find_arial():
    """Return (fontname, fontfile_or_None): Arial if present on this OS, else helv."""
    candidates = {
        "Windows": [r"C:\Windows\Fonts\arial.ttf"],
        "Darwin":  ["/Library/Fonts/Arial.ttf"],
        "Linux":   [
            "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ],
    }
    for path in candidates.get(platform.system(), []):
        if Path(path).exists():
            label = "Arial" if "arial" in path.lower() else "LiberSans"
            return label, path
    return "helv", None  # built-in Helvetica — visually equivalent to Arial


def add_watermark(doc: fitz.Document) -> None:
    fontname, fontfile = _find_arial()
    for page in doc:
        rect = page.rect

        # Dynamic font size so text width == 35% of page width
        if fontfile:
            font_obj = fitz.Font(fontfile=fontfile)
            unit_width = font_obj.text_length(WATERMARK_TEXT, fontsize=1)
        else:
            unit_width = fitz.get_text_length(
                WATERMARK_TEXT, fontname=fontname, fontsize=1
            )
        fontsize = (rect.width * WATERMARK_SCALE) / unit_width
        text_width = unit_width * fontsize

        # Watermark center: -0.1" left of center, 3.5" below center
        cx = rect.width  / 2 + WATERMARK_HORIZ_OFFSET
        cy = rect.height / 2 + WATERMARK_VERT_OFFSET

        # Insertion point: baseline-left of unrotated text, visually centered at (cx, cy)
        origin = fitz.Point(cx - text_width / 2, cy + fontsize * 0.3)

        page.insert_text(
            origin,
            WATERMARK_TEXT,
            fontname=fontname,
            fontfile=fontfile,
            fontsize=fontsize,
            color=WATERMARK_COLOR,
            fill_opacity=WATERMARK_FILL_OPACITY,
            morph=(fitz.Point(cx, cy), fitz.Matrix(WATERMARK_ROTATION)),
            overlay=False,  # appear behind page content
        )


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------
def process_pdf(
    pdf_path: Path, output_dir: Path, *, dry_run: bool, overwrite: bool
) -> bool:
    """
    Returns True if the file was processed (or would be in dry-run).
    Raises ValueError on missing student name, fitz.FileDataError on corrupt PDF.
    """
    doc = fitz.open(str(pdf_path))
    try:
        name = extract_student_name(doc)
        if not name:
            raise ValueError("no 'Student Name' label found")
        stem = safe_filename(f"{to_last_first(name)}, Transcript")
        out_path = output_dir / f"{stem}.pdf"
        print(f"   Student : {name}")
        print(f"   Output  : {out_path.name}")
        if dry_run:
            print("   (dry run)\n")
            return True
        if out_path.exists() and not overwrite:
            print("   SKIP    : output already exists (use --overwrite)\n")
            return False
        add_watermark(doc)
        doc.save(str(out_path), garbage=4, deflate=True)
        print("   Done.\n")
        return True
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="Folder containing PDF transcripts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned output names without writing files",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files in output/",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Error: not a directory: {folder}")

    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDF files found in {folder}")

    output_dir = folder / OUTPUT_DIRNAME
    if not args.dry_run:
        output_dir.mkdir(exist_ok=True)

    print(f"Found {len(pdfs)} PDF(s) in {folder}")
    if args.dry_run:
        print("Dry run — no files will be written.\n")

    skipped = []
    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name}")
        try:
            ok = process_pdf(
                pdf_path, output_dir, dry_run=args.dry_run, overwrite=args.overwrite
            )
            if not ok:
                skipped.append(pdf_path.name)
        except ValueError as e:
            print(f"   SKIP: {e}\n")
            skipped.append(pdf_path.name)
        except fitz.FileDataError as e:
            print(f"   ERROR (corrupt PDF): {e}\n")
            skipped.append(pdf_path.name)
        except Exception as e:
            print(f"   ERROR: {e}\n")
            skipped.append(pdf_path.name)

    processed = len(pdfs) - len(skipped)
    print(f"Finished. {processed}/{len(pdfs)} file(s) processed successfully.")
    if skipped:
        print(f"\n{len(skipped)} file(s) skipped or errored:")
        for s in skipped:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
