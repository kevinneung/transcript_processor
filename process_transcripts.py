"""
process_transcripts.py
-----------------------
Batch-process a folder of PDF transcripts on Windows, fully locally.

For each PDF in the folder it will:
  1. Read the "Student Name:" value from the page text (no network calls).
  2. Apply a text watermark via Adobe Acrobat COM automation,
     reproducing your saved "official" preset settings (set them in
     WATERMARK_SETTINGS below).
  3. Save and rename the file to "Last, First, Final Transcript.pdf".

Nothing is uploaded anywhere. Acrobat runs locally; pdfplumber reads locally.

REQUIREMENTS
  - Adobe Acrobat Pro (not Reader) installed and activated.
  - pip install pywin32 pdfplumber
  - Run from the same Windows user account that owns the Acrobat license.

USAGE
  python process_transcripts.py "C:\\path\\to\\folder_of_pdfs"
  python process_transcripts.py "C:\\path\\to\\folder" --dry-run
"""

import argparse
import re
import sys
import time
from pathlib import Path

import pdfplumber

try:
    import win32com.client as win32
except ImportError:
    win32 = None


# ---------------------------------------------------------------------------
# WATERMARK SETTINGS  --  fill these in to match your saved "official" preset.
# Open Acrobat > Edit > Watermark > Add, load your "official" preset, and copy
# each value from the dialog into the fields below.
#
# Acrobat JS reference for addWatermarkFromText parameters:
#   https://opensource.adobe.com/dc-acrobat-sdk-docs/library/jsapiref/
# ---------------------------------------------------------------------------
WATERMARK_SETTINGS = {
    "text": "Official",          # the watermark text
    "fontSize": 71,              # points
    "opacity": 1.0,             # 0.0 (transparent) to 1.0 (opaque)
    "rotation": 45,              # degrees, counter-clockwise
    # Color is RGB, each channel 0.0-1.0. Example below is a muted red.
    "color_r": 0.83,
    "color_g": 0.83,
    "color_b": 0.83,
    # Horizontal alignment: 0=left, 1=center, 2=right
    "horizAlign": 1,
    # Vertical alignment: 0=top, 1=center, 2=bottom
    "vertAlign": 1,
    # Offsets in points from the chosen alignment anchor
    "horizOffset": -.01,
    "vertOffset": -3.5,
    # Font name as Acrobat expects it, e.g. "Helvetica", "Times-Roman", "Courier"
    "fontName": "Arial",
}


# ---------------------------------------------------------------------------
# Student-name extraction
# ---------------------------------------------------------------------------
# Matches "Student Name: John Smith" or "Student Name John Smith" etc.
NAME_LABEL_PATTERN = re.compile(
    r"Student\s*Name\s*[:\-]?\s*(.+)", re.IGNORECASE
)


def extract_student_name(pdf_path: Path) -> str | None:
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = [line.strip() for line in text.splitlines()]

            for i, line in enumerate(lines):
                if "Student Name" in line:

                    # The actual student data is on the next line
                    if i + 1 < len(lines):
                        student_line = lines[i + 1]

                        # Parse: "Smith, Jack Fynn 12 ..."
                        m = re.match(
                            r"^([^,]+),\s+([A-Za-z'-]+)",
                            student_line
                        )

                        if m:
                            last = m.group(1).strip()
                            first = m.group(2).strip()

                            return f"{first} {last}"

    return None


def to_last_first(full_name: str) -> str:
    """
    Convert "First Middle Last" -> "Last, First".
    Falls back gracefully on single-token names.
    """
    parts = full_name.split()
    if len(parts) == 1:
        return parts[0]
    last = parts[-1]
    first = parts[0]
    return f"{last}, {first}"


def safe_filename(stem: str) -> str:
    """Strip characters Windows disallows in filenames."""
    return re.sub(r'[<>:"/\\|?*]', "", stem).strip()


# ---------------------------------------------------------------------------
# Acrobat COM automation
# ---------------------------------------------------------------------------
def build_watermark_js(s: dict) -> str:
    """Build the Acrobat document-level JavaScript to apply the watermark."""
    return f"""
    this.addWatermarkFromText({{
        cText: {s['text']!r},
        nTextAlign: app.constants.align.center,
        nHorizAlign: {s['horizAlign']},
        nVertAlign: {s['vertAlign']},
        nFontSize: {s['fontSize']},
        cFont: {s['fontName']!r},
        aColor: ["RGB", {s['color_r']}, {s['color_g']}, {s['color_b']}],
        nOpacity: {s['opacity']},
        nRotation: {s['rotation']},
        nHorizValue: {s['horizOffset']},
        nVertValue: {s['vertOffset']},
        bOnTop: true,
        bOnScreen: true,
        bOnPrint: true
    }});
    """


def process_with_acrobat(pdf_path: Path, output_path: Path, settings: dict):
    """Open in Acrobat, apply watermark via JS, save to output_path."""
    if win32 is None:
        raise RuntimeError(
            "pywin32 is not installed. Run: pip install pywin32"
        )

    avDoc = win32.Dispatch("AcroExch.AVDoc")
    if not avDoc.Open(str(pdf_path), ""):
        raise RuntimeError(f"Acrobat failed to open {pdf_path}")

    try:
        pdDoc = avDoc.GetPDDoc()
        pdDoc = avDoc.GetPDDoc()

        pdDoc.AddWatermarkFromText(
            settings["text"],
            0,   # rotation (ignored in some versions)
            settings["fontSize"],
            settings["opacity"],
            False,  # isFront
            False   # isOnPrint
        )

        pdDoc.Save(1, str(output_path))

    finally:
        avDoc.Close(True)


# ---------------------------------------------------------------------------
# Main batch loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="Folder containing PDF transcripts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read names and show planned renames without touching Acrobat",
    )
    parser.add_argument(
        "--suffix",
        default="Final Transcript",
        help='Filename suffix (default: "Final Transcript")',
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs found in {folder}")

    print(f"Found {len(pdfs)} PDF(s).\n")

    for i, pdf_path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf_path.name}")

        name = extract_student_name(pdf_path)
        if not name:
            print("   !! Could not find a Student Name. Skipping.\n")
            continue

        last_first = to_last_first(name)
        new_stem = safe_filename(f"{last_first}, {args.suffix}")
        output_path = pdf_path.with_name(new_stem + ".pdf")

        print(f"   Student: {name}")
        print(f"   Rename : {output_path.name}")

        if args.dry_run:
            print("   (dry run, no changes)\n")
            continue

        try:
            # Apply watermark to a temp output, then remove the original.
            tmp_out = pdf_path.with_name(pdf_path.stem + "__wm.pdf")
            process_with_acrobat(pdf_path, tmp_out, WATERMARK_SETTINGS)
            time.sleep(0.5)  # let Acrobat release the file handle
            pdf_path.unlink()
            tmp_out.rename(output_path)
            print("   Done.\n")
        except Exception as e:
            print(f"   !! Error: {e}\n")

    print("All files processed.")


if __name__ == "__main__":
    main()