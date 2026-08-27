"""Analysis tools — Tier 3 (analyze_code) + Tier 4 (find_references, rename_symbol, get_coverage)."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Optional


# ── Tier 3 ───────────────────────────────────────────────────────────────────

def t_analyze_code(path: Path) -> dict:
    """Deep AST analysis reusing PythonCodeAnalyzer from src/services/code_analyzer.py."""
    try:
        from ..services.code_analyzer import PythonCodeAnalyzer
    except ImportError:
        return {"error": "PythonCodeAnalyzer not available — check src/services/code_analyzer.py"}

    analyzer = PythonCodeAnalyzer()
    if not path.exists():
        return {"error": f"Path not found: {path}"}

    if path.is_file():
        info = analyzer.analyze_file(str(path))
        if not info:
            return {"error": f"Could not parse {path}"}
        return {
            "type": "file",
            "name": info.name,
            "path": info.file_path,
            "line_count": info.line_count,
            "docstring": info.docstring,
            "imports": info.imports[:50],
            "classes": [
                {
                    "name": c.name,
                    "line_start": c.line_start,
                    "line_end": c.line_end,
                    "methods": c.methods,
                    "base_classes": c.base_classes,
                    "docstring": c.docstring,
                    "is_test": c.is_test,
                    "complexity": c.complexity,
                }
                for c in info.classes
            ],
            "functions": [
                {
                    "name": f.name,
                    "line_start": f.line_start,
                    "line_end": f.line_end,
                    "parameters": f.parameters,
                    "returns": f.returns,
                    "docstring": f.docstring,
                    "is_test": f.is_test,
                    "complexity": f.complexity,
                }
                for f in info.functions
            ],
        }

    # Directory analysis
    result = analyzer.analyze_directory(str(path), max_files=200)
    return {
        "type": "directory",
        "path": str(path),
        "total_lines": result.total_lines,
        "total_files": len(result.modules),
        "total_classes": result.total_classes,
        "total_functions": result.total_functions,
        "total_tests": result.total_tests,
        "test_coverage_percent": round(result.test_coverage_percent, 1),
        "modules": [
            {
                "name": m.name,
                "file_path": m.file_path,
                "line_count": m.line_count,
                "class_count": len(m.classes),
                "function_count": len(m.functions),
            }
            for m in result.modules[:50]
        ],
        "classes": [
            {"name": c.name, "file": c.file_path, "line": c.line_start, "methods": len(c.methods)}
            for c in result.classes[:100]
        ],
        "dependencies": result.dependencies,
        "untested_classes": [
            c.name for c in result.classes
            if not c.is_test and c.name not in {
                t.tests_class for t in result.tests if t.tests_class
            }
        ][:50],
    }


# ── Tier 4 ───────────────────────────────────────────────────────────────────

_REF_CAP = 200
_SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}


def t_find_references(symbol: str, root: Path) -> dict:
    """Grep for whole-word occurrences of symbol across the workspace."""
    if not symbol:
        return {"error": "symbol must not be empty"}

    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    results = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        for fname in files:
            fpath = Path(dirpath) / fname
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        results.append({
                            "file": str(fpath),
                            "line": i,
                            "text": line.strip()[:200],
                        })
                        if len(results) >= _REF_CAP:
                            return {
                                "symbol": symbol,
                                "count": len(results),
                                "truncated": True,
                                "references": results,
                            }
            except Exception:
                continue
    return {"symbol": symbol, "count": len(results), "references": results}


def t_rename_symbol(old_name: str, new_name: str, root: Path) -> dict:
    """Whole-word search-and-replace across text files in root."""
    if not old_name or not new_name:
        return {"error": "old_name and new_name must not be empty"}

    pattern = re.compile(r'\b' + re.escape(old_name) + r'\b')
    modified: list[dict] = []
    errors: list[str] = []

    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        for fname in files:
            fpath = Path(dirpath) / fname
            try:
                original = fpath.read_text(encoding="utf-8", errors="ignore")
                count = len(pattern.findall(original))
                if count:
                    updated = pattern.sub(new_name, original)
                    fpath.write_text(updated, encoding="utf-8")
                    modified.append({"file": str(fpath), "replacements": count})
            except Exception as e:
                errors.append(f"{fpath}: {e}")

    return {
        "old_name": old_name,
        "new_name": new_name,
        "files_modified": len(modified),
        "total_replacements": sum(m["replacements"] for m in modified),
        "modified": modified,
        "errors": errors,
    }


def t_get_coverage(path: Path, workspace_root: str) -> dict:
    """Run 'coverage report' or read coverage.xml."""
    wr = workspace_root or str(path)

    # Try coverage.py CLI first
    try:
        r = subprocess.run(
            ["python", "-m", "coverage", "report", "--show-missing", f"--include={path}*"],
            capture_output=True, text=True, cwd=wr, timeout=30
        )
        if r.returncode == 0 and r.stdout.strip():
            return {"source": "coverage report", "output": r.stdout[:8000]}
    except Exception:
        pass

    # Try reading coverage.xml
    xml_path = Path(wr) / "coverage.xml"
    if xml_path.exists():
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(str(xml_path))
            root_el = tree.getroot()
            packages = []
            for pkg in root_el.iter("package"):
                packages.append({
                    "name": pkg.get("name", ""),
                    "line_rate": float(pkg.get("line-rate", 0)),
                    "branch_rate": float(pkg.get("branch-rate", 0)),
                })
            return {
                "source": "coverage.xml",
                "line_rate": root_el.get("line-rate"),
                "packages": packages,
            }
        except Exception as e:
            return {"error": f"Could not parse coverage.xml: {e}"}

    return {
        "error": "No coverage data found. Run 'pytest --cov' or 'coverage run' first.",
        "tip": "Install with: pip install pytest-cov coverage",
    }
