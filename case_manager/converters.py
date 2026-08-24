"""Word -> PDF conversion via a headless LibreOffice install, used so that
.docx/.doc source files can be viewed through the same PDF rendering path
as native PDFs.
"""
import shutil
import subprocess
from pathlib import Path

SOFFICE_BIN = shutil.which("soffice") or shutil.which("libreoffice")


class ConversionError(RuntimeError):
    pass


def convert_to_pdf(src_path: Path, out_dir: Path) -> Path:
    """Convert src_path (.docx/.doc) to a PDF inside out_dir, returning its path."""
    if SOFFICE_BIN is None:
        raise ConversionError(
            "LibreOffice ('soffice') was not found on this machine, so Word "
            "documents can't be converted automatically. Install it with "
            "`brew install --cask libreoffice`, or convert the file to PDF "
            "yourself and place it in documents/ with the same base name "
            f"(e.g. {src_path.stem}.pdf)."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        SOFFICE_BIN,
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(src_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(f"LibreOffice conversion timed out for {src_path.name}") from exc

    expected = out_dir / (src_path.stem + ".pdf")
    if result.returncode != 0 or not expected.exists():
        raise ConversionError(
            f"LibreOffice conversion failed for {src_path.name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return expected
