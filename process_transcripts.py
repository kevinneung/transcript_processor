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
def get_page_student_name(page: fitz.Page) -> str | None:
    """Return the student name if this page starts a new student section, else None."""
    lines = [l.strip() for l in page.get_text().splitlines() if l.strip()]

    # Strategy 1: "Student Name: ..." label
    for line in lines:
        m = NAME_LABEL_PATTERN.search(line)
        if m:
            name = m.group(1).strip()
            name = re.split(r"\s{2,}|\bDate\b|\bID\b", name)[0].strip()
            if name:
                return name

    # Strategy 2: line immediately before "Crs-ID" in "Last, First" format
    for i, line in enumerate(lines):
        if line == "Crs-ID" and i > 0:
            candidate = lines[i - 1]
            if NAME_INLINE_PATTERN.match(candidate):
                return candidate

    return None


def find_student_sections(doc: fitz.Document) -> list[tuple[str, list[int]]]:
    """
    Return a list of (name, [page_indices]) — one entry per student found in the doc.
    A new section starts only when the detected name differs from the current section's
    name, so continuation pages that repeat the header are grouped correctly.
    Pages before the first detected name are prepended to the first student's pages.
    """
    sections: list[list] = []   # each entry: [name, [page_indices]]
    leading: list[int] = []

    for page_num in range(len(doc)):
        name = get_page_student_name(doc[page_num])
        if name:
            current_name = sections[-1][0] if sections else None
            if name != current_name:
                # Genuinely new student
                sections.append([name, [page_num]])
            else:
                # Same student — continuation page with a repeated header
                sections[-1][1].append(page_num)
        elif sections:
            sections[-1][1].append(page_num)
        else:
            leading.append(page_num)

    if leading and sections:
        sections[0][1] = leading + sections[0][1]

    return [(name, pages) for name, pages in sections]


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
) -> tuple[int, int]:
    """
    Split pdf_path by student and write one output PDF per student.
    Returns (processed_count, skipped_count).
    Raises ValueError if no students found, fitz.FileDataError on corrupt PDF.
    """
    doc = fitz.open(str(pdf_path))
    try:
        sections = find_student_sections(doc)
        if not sections:
            raise ValueError("no 'Student Name' label found")

        processed = skipped = 0
        for name, page_indices in sections:
            stem = safe_filename(f"{to_last_first(name)}, Transcript")
            out_path = output_dir / f"{stem}.pdf"
            print(f"   Student : {name}  ({len(page_indices)} page(s))")
            print(f"   Output  : {out_path.name}")
            if dry_run:
                print("   (dry run)")
                processed += 1
                continue
            if out_path.exists() and not overwrite:
                print("   SKIP    : output already exists (use --overwrite)")
                skipped += 1
                continue

            # Build a sub-document for this student and watermark it
            subdoc = fitz.open()
            subdoc.insert_pdf(doc, from_page=page_indices[0], to_page=page_indices[-1])
            # Handle non-contiguous pages (rare, but safe)
            if page_indices != list(range(page_indices[0], page_indices[-1] + 1)):
                subdoc = fitz.open()
                for pg in page_indices:
                    subdoc.insert_pdf(doc, from_page=pg, to_page=pg)

            add_watermark(subdoc)
            subdoc.save(str(out_path), garbage=4, deflate=True)
            subdoc.close()
            print("   Done.")
            processed += 1

        print()
        return processed, skipped
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

    total_processed = total_skipped = 0
    errored = []
    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name}")
        try:
            done, skipped = process_pdf(
                pdf_path, output_dir, dry_run=args.dry_run, overwrite=args.overwrite
            )
            total_processed += done
            total_skipped += skipped
        except ValueError as e:
            print(f"   SKIP: {e}\n")
            errored.append(pdf_path.name)
        except fitz.FileDataError as e:
            print(f"   ERROR (corrupt PDF): {e}\n")
            errored.append(pdf_path.name)
        except Exception as e:
            print(f"   ERROR: {e}\n")
            errored.append(pdf_path.name)

    print(
        f"Finished. {total_processed} student PDF(s) written, "
        f"{total_skipped} skipped (already exist), "
        f"{len(errored)} input file(s) errored."
    )
    if errored:
        print(f"\nFiles with errors:")
        for s in errored:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
