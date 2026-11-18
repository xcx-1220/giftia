"""Isolated extractor for the web gateway's text-PDF attachment lane."""

import io
import json
import sys

MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 12
MAX_TEXT = 16_000


def extract(data):
    from pypdf import PdfReader

    if not data.startswith(b"%PDF-"):
        raise ValueError("Expected a PDF")
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("Unsupported page count or encrypted PDF")
    remaining = MAX_TEXT
    pages = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        remaining -= len(text)
        if remaining < 0:
            raise ValueError("PDF text is too long")
        pages.append(text)
    if not any(pages):
        raise ValueError("PDF has no text layer")
    return pages


if __name__ == "__main__":
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("PDF is too large")
    json.dump(extract(data), sys.stdout, ensure_ascii=True)
