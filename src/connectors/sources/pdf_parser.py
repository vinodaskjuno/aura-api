"""PDF parser connector — extracts text from PDF documents for ontology ingestion."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..base import AbstractConnector, SyncResult


class PdfParserConnector(AbstractConnector):
    """
    Config keys:
      path: str             path to a single PDF or directory of PDFs
      label: str            default 'Document'
      max_pages: int        default 50
    """

    def test_connection(self) -> tuple[bool, str]:
        try:
            import pypdf  # noqa: F401
            path = Path(self.config.get("path", ""))
            if path.exists():
                return True, f"pypdf available, path exists: {path}"
            return False, f"Path not found: {path}"
        except ImportError:
            return False, "pypdf not installed — run: pip install pypdf"

    def sync(self) -> SyncResult:
        result = SyncResult()
        for doc in self._parse_all():
            result.entities_added += 1
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        return self._parse_all()[:3]

    def _parse_all(self) -> list[dict[str, Any]]:
        path = Path(self.config.get("path", ""))
        if path.is_file():
            pdfs = [path]
        elif path.is_dir():
            pdfs = list(path.rglob("*.pdf"))
        else:
            return []
        docs = []
        for pdf_path in pdfs[:20]:
            try:
                text = self._extract_text(pdf_path)
                docs.append({
                    "filename": pdf_path.name,
                    "path": str(pdf_path),
                    "label": self.config.get("label", "Document"),
                    "text_excerpt": text[:500],
                    "char_count": len(text),
                })
            except Exception:
                pass
        return docs

    def _extract_text(self, path: Path) -> str:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        max_pages = self.config.get("max_pages", 50)
        return "\n".join(
            page.extract_text() or ""
            for page in reader.pages[:max_pages]
        )
