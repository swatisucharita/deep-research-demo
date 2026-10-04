"""Read PDF and text files from the docs folder."""

from pathlib import Path

from pypdf import PdfReader

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".rst"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | PDF_EXTENSIONS


def list_documents(docs_dir: Path) -> list[Path]:
    """Return supported files in docs_dir (recursive), relative to docs_dir."""
    if not docs_dir.exists():
        return []
    return sorted(
        p.relative_to(docs_dir)
        for p in docs_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def resolve_document(docs_dir: Path, name: str) -> Path:
    """Resolve a file name inside docs_dir, refusing paths that escape it."""
    root = docs_dir.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"'{name}' is outside the docs folder")
    if not path.is_file():
        raise FileNotFoundError(f"'{name}' not found in docs folder")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return path


def read_document(path: Path) -> str:
    """Extract text from a PDF or plain-text file."""
    if path.suffix.lower() in PDF_EXTENSIONS:
        reader = PdfReader(path)
        pages = [
            f"--- page {i} ---\n{page.extract_text() or ''}"
            for i, page in enumerate(reader.pages, start=1)
        ]
        return "\n\n".join(pages)
    return path.read_text(encoding="utf-8", errors="replace")
