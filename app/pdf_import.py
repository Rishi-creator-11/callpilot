"""Bounded extraction of plain text from uploaded PDFs."""
import io
import os
from pathlib import Path
import subprocess
import sys
from threading import BoundedSemaphore

from fastapi import HTTPException

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 12000
MAX_PAGES = 50
PDF_WORKERS = BoundedSemaphore(2)


def extract_pdf(data: bytes) -> str:
    if not data.startswith(b"%PDF-"):
        raise HTTPException(422, "Choose a valid PDF document.")
    if not PDF_WORKERS.acquire(blocking=False):
        raise HTTPException(429, "PDF imports are busy. Try again in a moment.")
    try:
        result = subprocess.run(
            [sys.executable, "-P", str(Path(__file__).resolve())],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(422, "This PDF took too long to read. Try a smaller document.") from None
    finally:
        PDF_WORKERS.release()
    if result.returncode == 2:
        raise HTTPException(422, "Use an unlocked PDF with at most 50 pages.")
    if result.returncode == 3:
        raise HTTPException(422, "This PDF has too much text for one source. Split it into shorter documents.")
    if result.returncode:
        raise HTTPException(422, "This PDF could not be read. Try saving a fresh copy.")
    text = result.stdout.decode("utf-8")
    if len(text) < 20:
        raise HTTPException(422, "This PDF has too little selectable text. Use a text-based PDF or paste the content.")
    return text


def _worker() -> int:
    # Apply limits in the child before loading the parser or opening untrusted data.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
    from pypdf import PdfReader

    try:
        document = PdfReader(io.BytesIO(sys.stdin.buffer.read(MAX_PDF_BYTES + 1)), strict=True)
        if document.is_encrypted or len(document.pages) > MAX_PAGES:
            return 2
        parts = []
        length = 0
        for page in document.pages:
            text = "\n".join(line.strip() for line in page.extract_text().splitlines())
            text = "".join(char for char in text if char.isprintable() or char in "\n\t").strip()
            if text:
                parts.append(text)
                length += len(text) + 2
                if length > MAX_TEXT_CHARS + 2:
                    return 3
        sys.stdout.write("\n\n".join(parts))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(_worker())
