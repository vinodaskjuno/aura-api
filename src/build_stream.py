"""Run a full build, emitting one JSON progress event per line.

Events go to a progress file (argv[2]) when provided — the server async-tails that
file, which is far more robust on Windows than reading a subprocess stdout pipe.
Falls back to stdout for CLI use.
"""
import json
import sys

from .pipeline import run_full_build


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    prog_path = sys.argv[2] if len(sys.argv) > 2 else None
    fh = open(prog_path, "a", encoding="utf-8") if prog_path else None

    def emit(ev: dict):
        s = json.dumps(ev)
        if fh:
            fh.write(s + "\n")
            fh.flush()
        else:
            sys.stdout.write(s + "\n")
            sys.stdout.flush()

    try:
        run_full_build(path, progress=emit)
    except Exception as e:
        emit({"type": "error", "message": str(e)})
    finally:
        emit({"type": "__end__"})
        if fh:
            fh.close()


if __name__ == "__main__":
    main()
