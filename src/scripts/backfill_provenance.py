"""Mark pre-existing nodes and edges as written before tracing began.

Everything already in the graph predates `src/graph/provenance.py`, so none of it
carries an actor, a run, or a trigger. That information does not exist anywhere and
cannot be recovered — the writes that would have recorded it have already happened.

So this script does not invent it. It derives `pipeline` from the `source` string
each writer was already setting, carries `createdAt`/`updatedAt` across to
`firstSeenAt`/`lastSeenAt`, and marks the entity `attribution: "pre-trace"`.

That marking is the point. Onto Verse renders a pre-trace entity as an explicit
"origin not recorded" state rather than a blank panel, and the coverage meter on the
Lineage page counts it against the total — so the gap is a visible, shrinking number
instead of an invisible one. Guessing an actor would erase that signal and replace it
with a plausible lie.

    python -m src.scripts.backfill_provenance --dry-run
    python -m src.scripts.backfill_provenance
    python -m src.scripts.backfill_provenance --batch 2000
"""
from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill-provenance")

# Maps the `source` values already in the graph onto the pipeline vocabulary the UI
# groups and colours by. Anything unlisted stays "unattributed", which is honest:
# an unrecognised source is exactly a thing whose origin we cannot classify.
SOURCE_TO_PIPELINE = {
    "git": "git",
    "git-repo-loader": "git",
    "code-analysis": "dev-mate",
    "qa-test": "qa-mind",
    "observability": "self-learning",
    "manual": "manual",
    "chat": "manual",
    "bulk_load": "mcp",
    "mock_mcp": "mcp",
    "file_upload": "file-upload",
    "api_ingest": "api",
    "servicenow": "api",
    "servicenow_cmdb": "api",
    "servicenow_change": "api",
    "wiz": "api",
    "kubernetes": "api",
    "pagerduty": "api",
    "confluence": "api",
    "correlation": "correlation",
    "seed": "seed",
    "scheduler": "mcp",
}


def _pipeline_for(source: str) -> str:
    if not source:
        return "unattributed"
    if source.startswith("mcp:"):
        return "mcp"
    return SOURCE_TO_PIPELINE.get(source, "unattributed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    ap.add_argument("--batch", type=int, default=1000,
                    help="nodes per write transaction")
    ap.add_argument("--edges", action="store_true", default=True,
                    help="backfill relationships as well as nodes (default: yes)")
    args = ap.parse_args()

    from src.graph import neo4j_client as graph

    if not graph.is_available():
        log.error("No graph backend is available — nothing to backfill.")
        return 1

    # Only entities that have not already been stamped. Re-running is therefore
    # cheap and safe: a second pass finds nothing, and a run interrupted halfway
    # resumes where it stopped rather than starting over.
    counts = graph.run_query("""
        MATCH (n) WHERE n.attribution IS NULL
        RETURN coalesce(n.source, '') AS source, count(*) AS count
        ORDER BY count DESC
    """)
    total = sum(row["count"] for row in counts)
    if not total:
        log.info("Every node is already stamped — nothing to do.")
    else:
        log.info("%d node(s) to backfill:", total)
        for row in counts:
            log.info("  %-24s %6d  ->  %s",
                     row["source"] or "(no source)", row["count"],
                     _pipeline_for(row["source"]))

    edge_total = 0
    if args.edges:
        edge_rows = graph.run_query("""
            MATCH ()-[r]->() WHERE r.attribution IS NULL
            RETURN count(r) AS count
        """)
        edge_total = edge_rows[0]["count"] if edge_rows else 0
        log.info("%d relationship(s) to backfill", edge_total)

    if args.dry_run:
        log.info("--dry-run: nothing written.")
        return 0

    # Batched, and looped until it stops finding work. A single transaction over a
    # few hundred thousand nodes would exhaust the 1 GB heap the Neo4j container
    # runs with — the same reason graph/dialects batches bulk delete.
    #
    # One statement per distinct `source`, with the pipeline resolved in Python.
    # Cypher can index a map with a dynamic key, but the two engines do not agree
    # on it, and a portability bug in a one-off migration is a poor trade for
    # saving twenty round trips.
    def _sweep(match: str, var: str, sources: list[str]) -> int:
        done_total = 0
        for source in sources:
            pipeline = _pipeline_for(source)
            while True:
                rows = graph.run_query(f"""
                    {match} WHERE {var}.attribution IS NULL
                      AND coalesce({var}.source, '') = $source
                    WITH {var} LIMIT $batch
                    SET {var}.attribution = 'pre-trace',
                        {var}.pipeline = $pipeline,
                        {var}.trigger = 'unknown',
                        {var}.firstSeenAt = coalesce({var}.firstSeenAt, {var}.firstSeen,
                                                     {var}.createdAt, {var}.updatedAt),
                        {var}.lastSeenAt = coalesce({var}.lastSeenAt, {var}.lastSeen,
                                                    {var}.updatedAt, {var}.createdAt)
                    RETURN count({var}) AS done
                """, {"batch": args.batch, "source": source, "pipeline": pipeline})
                done = rows[0]["done"] if rows else 0
                done_total += done
                if done:
                    log.info("  … %-24s %6d  (%s)", source or "(no source)",
                             done_total, pipeline)
                if done < args.batch:
                    break
        return done_total

    node_sources = [row["source"] for row in counts]
    written = _sweep("MATCH (n)", "n", node_sources)

    edges_written = 0
    if args.edges:
        edge_sources = [row["source"] for row in graph.run_query("""
            MATCH ()-[r]->() WHERE r.attribution IS NULL
            RETURN coalesce(r.source, '') AS source, count(*) AS count
            ORDER BY count DESC
        """)]
        edges_written = _sweep("MATCH ()-[r]->()", "r", edge_sources)

    log.info("Backfilled %d node(s) and %d relationship(s) as 'pre-trace'.",
             written, edges_written)
    log.info("Everything written from now on is fully attributed; the Lineage page "
             "shows this as the partial slice of the coverage meter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
