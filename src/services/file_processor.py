"""Parse uploaded files into normalized text content for ontology extraction."""
import csv
import io
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".json", ".txt", ".yaml", ".yml", ".md", ".py", ".ts", ".js"}

IT_KEYWORDS = {
    "api", "service", "deploy", "pipeline", "build", "test", "repo", "git",
    "docker", "kubernetes", "ci", "cd", "release", "version", "commit",
    "branch", "function", "class", "module", "import", "require", "config",
    "env", "database", "schema", "endpoint", "auth", "token", "server",
    "client", "request", "response", "artifact", "dependency", "package",
    "workflow", "action", "stage", "job", "step", "runner", "container",
    "image", "registry", "cluster", "pod", "node", "namespace", "helm",
    "terraform", "ansible", "jenkins", "github", "gitlab", "azure", "aws",
    "gcp", "lambda", "ec2", "s3", "rds", "vpc", "subnet", "security",
    "monitor", "log", "metric", "alert", "dashboard", "trace",
}


@dataclass
class ProcessedFile:
    filename: str
    file_type: str
    content: str
    metadata: dict


def process_file(filename: str, raw_bytes: bytes) -> ProcessedFile:
    """Parse a file into normalized text content."""
    ext = _ext(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

    if ext in (".xlsx", ".xls"):
        content, meta = _parse_excel(raw_bytes, filename)
    elif ext == ".csv":
        content, meta = _parse_csv(raw_bytes)
    elif ext == ".json":
        content, meta = _parse_json(raw_bytes)
    else:
        content, meta = _parse_text(raw_bytes, filename)

    _guard_it_domain(content, filename)

    return ProcessedFile(filename=filename, file_type=ext.lstrip("."), content=content, metadata=meta)


def _ext(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


def _parse_excel(raw: bytes, filename: str) -> tuple[str, dict]:
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl not installed — run: pip install openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    lines = []
    total_rows = 0
    for sheet in wb.worksheets:
        lines.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            row_str = "\t".join(str(c) if c is not None else "" for c in row)
            if row_str.strip():
                lines.append(row_str)
                total_rows += 1
            if total_rows > 2000:
                lines.append("... (truncated)")
                break
    return "\n".join(lines), {"sheets": len(wb.worksheets), "rows": total_rows}


def _parse_csv(raw: bytes) -> tuple[str, dict]:
    text = raw.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    content = "\n".join("\t".join(r) for r in rows[:2000])
    return content, {"rows": len(rows)}


def _parse_json(raw: bytes) -> tuple[str, dict]:
    text = raw.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
        pretty = json.dumps(obj, indent=2)[:50_000]
        return pretty, {"size_bytes": len(raw)}
    except json.JSONDecodeError:
        return text[:50_000], {"size_bytes": len(raw), "parse_error": True}


def _parse_text(raw: bytes, filename: str) -> tuple[str, dict]:
    text = raw.decode("utf-8", errors="replace")
    return text[:50_000], {"size_bytes": len(raw), "filename": filename}


def _guard_it_domain(content: str, filename: str) -> None:
    sample = (content[:3000] + filename).lower()
    hits = sum(1 for kw in IT_KEYWORDS if kw in sample)
    if hits < 2:
        raise ValueError(
            "This file does not appear to contain IT/infrastructure/SDLC content. "
            "Only upload code, configuration, pipeline, architecture, or test-related files."
        )
