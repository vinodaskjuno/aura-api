"""CLI: `python -m src.qatest --project <id> --url <url>`

The same code path the API uses, so a CI run and a UI run produce identical evidence.
This is how a run is started on a machine that has podman — which the deployed
backend does not.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.qatest",
                                 description="Run QualityMind tests for a project.")
    ap.add_argument("--project", required=True, help="project id, as used in the graph")
    ap.add_argument("--url", required=True, help="base URL of the running application")
    ap.add_argument("--run-id", default=None, help="reuse a run id (default: generated)")
    ap.add_argument("--by", default="cli", help="who to record as having run it")
    ap.add_argument("--exploratory", action="store_true",
                    help="also run the AI browser pass")
    ap.add_argument("--no-graph", action="store_true",
                    help="skip writing results back to the knowledge graph")
    ap.add_argument("--probe", metavar="CLOUD",
                    help="start one emulator, check it answers, stop it, and exit")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    if args.probe:
        from src.qatest.emulators import probe
        r = probe(args.probe)
        print(json.dumps(r, indent=2) if args.json
              else f"{'OK  ' if r['ok'] else 'FAIL'} {args.probe}: {r['message']}")
        return 0 if r["ok"] else 1

    from src.qatest.service import execute

    def show(ev: dict) -> None:
        if not args.json:
            msg = ev.get("message") or ev.get("action") or ""
            print(f"  {ev['type']:9} {msg}", flush=True)

    report = execute(args.project, args.url, run_id=args.run_id, ran_by=args.by,
                     exploratory=args.exploratory, write_graph=not args.no_graph,
                     on_event=show)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\n  run {report['runId']}  {report['status']}")
        print(f"  passed {report['totalPassed']}  failed {report['totalFailed']}  "
              f"skipped {report['totalSkipped']}")
        if report.get("reason"):
            print(f"  reason: {report['reason']}")

    # Non-zero on failure so CI can gate on it; "unavailable" is 2, distinct from a
    # genuine test failure, because they need different responses.
    return {"passed": 0, "failed": 1, "unavailable": 2}.get(report["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
