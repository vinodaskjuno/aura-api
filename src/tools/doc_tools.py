"""Document generation tools — Tier 3 (generate_docstring, generate_readme, generate_api_docs)."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Optional


# ── Tier 3 ───────────────────────────────────────────────────────────────────

def t_generate_docstring(path: Path, function_name: str) -> dict:
    """Extract the source of a function/class so the LLM can write a proper docstring for it."""
    if not path.exists():
        return {"error": f"File not found: {path}"}

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"error": f"Syntax error in {path.name}: {e}"}

    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == function_name:
                start = node.lineno - 1
                end = node.end_lineno or (start + 1)
                snippet = "\n".join(lines[start:end])

                # Check if it already has a docstring
                existing_docstring = None
                if (node.body and isinstance(node.body[0], ast.Expr) and
                        isinstance(node.body[0].value, ast.Constant)):
                    existing_docstring = node.body[0].value.value

                # Extract signature
                if isinstance(node, ast.ClassDef):
                    sig = f"class {node.name}"
                    if node.bases:
                        bases = [ast.unparse(b) for b in node.bases]
                        sig += f"({', '.join(bases)})"
                else:
                    sig = ast.unparse(node)[:200]

                return {
                    "function": function_name,
                    "file": str(path),
                    "line_start": node.lineno,
                    "line_end": end,
                    "signature": sig,
                    "source": snippet[:3000],
                    "has_docstring": existing_docstring is not None,
                    "existing_docstring": existing_docstring,
                    "instruction": (
                        "Write a Google-style docstring for the above function/class. "
                        "Then use edit_file to insert it as the first line of the body."
                    ),
                }

    return {"error": f"'{function_name}' not found in {path.name}"}


def t_generate_readme(workspace_root: Path, output_path: Optional[Path]) -> dict:
    """Generate a README.md skeleton from workspace structure."""
    if output_path is None:
        output_path = workspace_root / "README.md"

    # Collect project info
    name = workspace_root.name
    description = ""
    tech_stack: list[str] = []
    entry_points: list[str] = []
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}

    # Detect tech stack
    if (workspace_root / "requirements.txt").exists():
        tech_stack.append("Python")
        reqs = (workspace_root / "requirements.txt").read_text(encoding="utf-8", errors="ignore")
        if "fastapi" in reqs.lower():
            tech_stack.append("FastAPI")
        if "django" in reqs.lower():
            tech_stack.append("Django")
        if "flask" in reqs.lower():
            tech_stack.append("Flask")
    if (workspace_root / "package.json").exists():
        tech_stack.append("Node.js")
        try:
            import json
            data = json.loads((workspace_root / "package.json").read_text(encoding="utf-8"))
            name = data.get("name", name)
            description = data.get("description", "")
            deps = data.get("dependencies", {})
            if "react" in deps:
                tech_stack.append("React")
            if "vue" in deps:
                tech_stack.append("Vue")
            if "express" in deps:
                tech_stack.append("Express")
        except Exception:
            pass

    # Find entry points
    for candidate in ["main.py", "app.py", "server.py", "index.js", "index.ts"]:
        if (workspace_root / candidate).exists():
            entry_points.append(candidate)
    if (workspace_root / "src" / "main.py").exists():
        entry_points.append("src/main.py")

    # Collect top-level structure
    top_level = []
    try:
        for entry in sorted(workspace_root.iterdir()):
            if entry.name not in skip and not entry.name.startswith("."):
                top_level.append(entry.name + ("/" if entry.is_dir() else ""))
    except Exception:
        pass

    # Build README content
    lines = [
        f"# {name}",
        "",
        f"> {description}" if description else "> TODO: Add project description",
        "",
        "## Tech Stack",
        "",
        ", ".join(tech_stack) if tech_stack else "TODO",
        "",
        "## Project Structure",
        "",
        "```",
    ]
    lines += [f"  {item}" for item in top_level[:30]]
    lines += [
        "```",
        "",
        "## Getting Started",
        "",
        "### Prerequisites",
        "",
        "- Python 3.9+ (or Node.js 18+)",
        "",
        "### Installation",
        "",
        "```bash",
        "# Python",
        "pip install -r requirements.txt" if (workspace_root / "requirements.txt").exists() else "# TODO",
        "# or Node.js",
        "npm install" if (workspace_root / "package.json").exists() else "",
        "```",
        "",
        "### Running",
        "",
        "```bash",
        f"python {entry_points[0]}" if entry_points and entry_points[0].endswith(".py") else "# TODO: add run command",
        "```",
        "",
        "## API",
        "",
        "See [docs/API.md](docs/API.md) for endpoint documentation.",
        "",
        "## Contributing",
        "",
        "1. Fork the repository",
        "2. Create a feature branch (`git checkout -b feature/my-feature`)",
        "3. Commit your changes (`git commit -m 'Add my feature'`)",
        "4. Push to the branch (`git push origin feature/my-feature`)",
        "5. Open a Pull Request",
        "",
        "## License",
        "",
        "TODO: Add license information",
    ]

    content = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    return {
        "written": str(output_path),
        "chars": len(content),
        "detected_stack": tech_stack,
        "entry_points": entry_points,
        "note": "Review and complete the TODO sections before publishing.",
    }


def t_generate_api_docs(workspace_root: Path, output_path: Optional[Path]) -> dict:
    """Parse FastAPI router decorators and generate Markdown API docs."""
    if output_path is None:
        docs_dir = workspace_root / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        output_path = docs_dir / "API.md"

    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
    endpoints: list[dict] = []

    # Regex patterns for FastAPI decorators
    decorator_re = re.compile(
        r'@(?:router|app)\.(get|post|put|patch|delete|head|options)\s*\(\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    func_re = re.compile(r'^\s*(?:async\s+)?def\s+(\w+)\s*\(', re.MULTILINE)
    docstring_re = re.compile(r'^\s+"""([^"]+)"""', re.MULTILINE)

    for dirpath, dirs, files in os.walk(workspace_root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            try:
                source = fpath.read_text(encoding="utf-8", errors="ignore")
                lines = source.splitlines()
                for i, line in enumerate(lines):
                    m = decorator_re.search(line)
                    if m:
                        method = m.group(1).upper()
                        path_val = m.group(2)
                        # Find function name on next non-empty line
                        func_name = ""
                        docstring = ""
                        for j in range(i + 1, min(i + 5, len(lines))):
                            fm = func_re.match(lines[j])
                            if fm:
                                func_name = fm.group(1)
                                # Look for docstring
                                if j + 1 < len(lines) and '"""' in lines[j + 1]:
                                    docstring = lines[j + 1].strip().strip('"')
                                break
                        endpoints.append({
                            "method": method,
                            "path": path_val,
                            "function": func_name,
                            "file": str(fpath.relative_to(workspace_root)),
                            "description": docstring,
                        })
            except Exception:
                continue

    # Sort by path then method
    endpoints.sort(key=lambda e: (e["path"], e["method"]))

    # Generate Markdown
    lines_out = [
        "# API Reference",
        "",
        f"Auto-generated from {len(endpoints)} endpoints. Last updated: see git log.",
        "",
        "| Method | Path | Function | Description |",
        "|--------|------|----------|-------------|",
    ]
    for ep in endpoints:
        desc = ep["description"][:80] if ep["description"] else "—"
        lines_out.append(
            f"| `{ep['method']}` | `{ep['path']}` | `{ep['function']}` | {desc} |"
        )

    lines_out += ["", "## Endpoint Details", ""]
    for ep in endpoints:
        lines_out += [
            f"### {ep['method']} `{ep['path']}`",
            "",
            f"**Function:** `{ep['function']}` in `{ep['file']}`",
            "",
            ep["description"] if ep["description"] else "_No description provided._",
            "",
        ]

    content = "\n".join(lines_out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    return {
        "written": str(output_path),
        "endpoints_found": len(endpoints),
        "chars": len(content),
    }
