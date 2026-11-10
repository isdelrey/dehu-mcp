"""PDF → text (pdftotext if installed, else pypdf)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def pdf_text(path: str | Path, layout: bool = True) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    if shutil.which("pdftotext"):
        args = ["pdftotext"] + (["-layout"] if layout else []) + [str(p), "-"]
        return subprocess.run(args, capture_output=True, text=True).stdout
    from pypdf import PdfReader
    return "\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
