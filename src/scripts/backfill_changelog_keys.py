"""Re-key ontology-changelog rows from engine node id to externalId.

History used to be keyed by Neo4j's elementId. That value does not survive a
database rebuild, and now that a deployment may run Neo4j or Memgraph it differs
per engine — so a row written against one store would not resolve against the
other. `entityId` is now the externalId.

Existing rows are still keyed the old way. The read path queries both, so nothing
is lost without this script; running it makes those rows resolve on either engine
rather than only on the store they were written against.

Rows whose externalId cannot be determined are left exactly as they are — a
changelog entry that cannot be re-keyed is better than one re-keyed to a guess.

    python -m src.scripts.backfill_changelog_keys --dry-run
    python -m src.scripts.backfill_changelog_keys
"""
from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    from src.database import dynamo_client as db
    from src.graph import neo4j_client as graph

    rows = db.scan_items("ontology-changelog", limit=args.limit)
    log.info("scanned %d changelog rows", len(rows))

    # Resolving each id against the graph one at a time would be a query per row.
    # One pass builds the whole map instead.
    id_to_external: dict[str, str] = {}
    if graph.is_available():
        try:
            for rec in graph.run_query(
                "MATCH (n) WHERE n.externalId IS NOT NULL "
                "RETURN elementId(n) AS id, n.externalId AS eid"):
                id_to_external[str(rec["id"])] = rec["eid"]
            log.info("resolved %d node ids from the active graph", len(id_to_external))
        except Exception as exc:  # noqa: BLE001
            log.warning("graph lookup failed, using row-local externalId only: %s", exc)
    else:
        log.warning("no graph available — only rows that already carry externalId "
                    "can be re-keyed")

    updated = skipped = already = 0
    for row in rows:
        change_id = row.get("changeId")
        entity_id = str(row.get("entityId") or "")
        if not change_id or not entity_id:
            skipped += 1
            continue

        # The row may already carry externalId (written after 707ec16), otherwise
        # fall back to resolving its engine id against the graph.
        external = row.get("externalId") or id_to_external.get(entity_id, "")
        if not external:
            skipped += 1
            continue
        if entity_id == external:
            already += 1
            continue

        updated += 1
        if args.dry_run:
            log.info("  %s: %s -> %s", change_id, entity_id[:28], external)
            continue
        db.update_item("ontology-changelog", {"changeId": change_id},
                       {"entityId": external, "elementId": entity_id,
                        "externalId": external})

    verb = "would re-key" if args.dry_run else "re-keyed"
    log.info("%s %d row(s); %d already portable; %d left as-is (no externalId)",
             verb, updated, already, skipped)
    if skipped:
        log.info("Rows left as-is still resolve — the read path queries both keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
