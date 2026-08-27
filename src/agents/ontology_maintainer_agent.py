"""LangGraph-based ontology maintainer agent — human-in-the-loop edition.

Streamed via WebSocket at /api/ontology/ws/chat.

Mutation protocol:
  1. Agent searches and inspects nodes as needed.
  2. Agent calls `propose_changes` to declare ALL planned mutations.
  3. The WS handler sends a `pending_change` frame to the frontend.
  4. User confirms or cancels via the confirmation card.
  5. The WS handler (not the agent) executes confirmed changes and writes logs.

This file never executes mutations directly — it only proposes them.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncGenerator

log = logging.getLogger(__name__)

# ── Read-only tool implementations ───────────────────────────────────────────

def _search_nodes(query: str, node_type: str | None = None) -> list[dict]:
    from src.graph import neo4j_client as neo4j
    return neo4j.search_nodes(query, node_type=node_type, limit=10)


def _get_node_details(node_id: str) -> dict | None:
    from src.graph import neo4j_client as neo4j
    return neo4j.get_node_by_id(node_id)


def _get_node_relationships(node_id: str) -> list[dict]:
    """Return relationships connected to this node."""
    from src.graph import neo4j_client as neo4j
    node = neo4j.get_node_by_id(node_id)
    if not node:
        return []
    cypher = """
    MATCH (n)-[r]-(m)
    WHERE elementId(n) = $id AND r.active <> false
    RETURN type(r) AS relType, elementId(r) AS relId,
           elementId(m) AS otherId, m.name AS otherName,
           labels(m)[0] AS otherLabel,
           startNode(r) = n AS isOutgoing
    LIMIT 50
    """
    try:
        with neo4j.session() as s:
            return [
                {
                    "relType": r["relType"],
                    "relId": r["relId"],
                    "otherId": r["otherId"],
                    "otherName": r["otherName"],
                    "otherLabel": r["otherLabel"],
                    "direction": "outgoing" if r["isOutgoing"] else "incoming",
                }
                for r in s.run(cypher, id=node_id)
            ]
    except Exception as exc:
        return [{"error": str(exc)}]


# ── Tool schemas ──────────────────────────────────────────────────────────────

_TOOLS_SCHEMA = [
    {
        "name": "search_nodes",
        "description": "Search for ontology nodes by name, hostname, or externalId. Use this first to find the exact node IDs you need before proposing changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term (name, hostname, or externalId)"},
                "node_type": {"type": "string", "description": "Optional filter: Service, Infrastructure, Project, Team, SecurityFinding, Incident, Repository, etc."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_node_details",
        "description": "Get all properties and labels of a specific node by its Neo4j elementId.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Neo4j elementId (from search_nodes result)"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "get_node_relationships",
        "description": "List all active relationships connected to a node — both incoming and outgoing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Neo4j elementId"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "propose_changes",
        "description": (
            "Propose ALL planned mutations for human confirmation. "
            "Call this INSTEAD of any direct mutation. "
            "The user will see a diff card and must approve before execution. "
            "List every individual change — one per property update, one per relationship. "
            "After calling this, stop — do not call any other tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Clear human-readable summary of all proposed changes (1-3 sentences).",
                },
                "changes": {
                    "type": "array",
                    "description": "List of individual change items.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "changeType": {
                                "type": "string",
                                "enum": ["UPDATE", "CREATE", "RETIRE", "RELATIONSHIP_ADD", "RELATIONSHIP_ARCHIVE"],
                                "description": "Type of change.",
                            },
                            "entityId": {
                                "type": "string",
                                "description": "Neo4j elementId of the node/relationship. Null for CREATE.",
                            },
                            "entityLabel": {
                                "type": "string",
                                "description": "Node label or relationship type.",
                            },
                            "entityName": {
                                "type": "string",
                                "description": "Human-readable name of the entity.",
                            },
                            "prop": {
                                "type": "string",
                                "description": "Property name (for UPDATE only).",
                            },
                            "before": {
                                "description": "Current value (for UPDATE) or null.",
                            },
                            "after": {
                                "description": "New value (for UPDATE/CREATE) or null.",
                            },
                            "relType": {
                                "type": "string",
                                "description": "Relationship type (for RELATIONSHIP_ADD/ARCHIVE).",
                            },
                            "toLabel": {
                                "type": "string",
                                "description": "Target node label (for RELATIONSHIP_ADD or CREATE).",
                            },
                            "toExternalId": {
                                "type": "string",
                                "description": "Target node externalId (for RELATIONSHIP_ADD).",
                            },
                        },
                        "required": ["changeType", "entityName", "entityLabel"],
                    },
                },
            },
            "required": ["summary", "changes"],
        },
    },
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Ontology Maintainer Agent for the Aura Enterprise Knowledge Graph.
You help authorized maintainers understand and modify the organizational ontology stored in Neo4j.

## Your tools
- search_nodes: find nodes by name, hostname, or ID
- get_node_details: inspect all properties of a specific node
- get_node_relationships: list active relationships on a node
- propose_changes: declare ALL planned mutations for human confirmation

## Mutation protocol (STRICT)
1. ALWAYS search for and inspect nodes before planning changes.
2. Show the user what you found (current state) before proposing changes.
3. Call propose_changes ONCE with ALL planned changes listed. NEVER call it multiple times.
4. After calling propose_changes, STOP — write a brief note saying changes await user approval.
5. NEVER attempt to execute mutations yourself. You only propose; the system executes after approval.

## What you can propose
- UPDATE: change a property on an existing node (one change item per property)
- CREATE: add a new node to the ontology
- RETIRE: soft-delete a node (sets status=retired, never hard-deleted)
- RELATIONSHIP_ADD: add a directed relationship between two existing nodes
- RELATIONSHIP_ARCHIVE: soft-delete a relationship (sets active=false)

## Rules
- Search first, propose second. Never propose a change to a node you haven't confirmed exists.
- For bulk operations (e.g. "update all services"), list each node as a separate change item.
- Be precise: include exact property names and values from your inspection.
- If you can't find a node the user mentioned, say so clearly — do not propose a change to a guessed ID.
- Prefer RETIRE over DELETE — permanent deletion is never allowed.
"""


# ── Agent loop ────────────────────────────────────────────────────────────────

async def run_maintainer_agent(
    user_text: str,
    user: dict,
    session_id: str,
) -> AsyncGenerator[dict, None]:
    """Async generator — yields WebSocket frame dicts.

    Frame types emitted:
      token         — streamed assistant text (field: text)
      tool_start    — tool invocation started (fields: tool, input)
      tool_end      — tool result (fields: tool, result)
      pending_change — proposed mutations awaiting user confirmation (fields: changeId, changes, summary)
      done          — agent turn complete
      error         — unrecoverable error (field: message)
    """
    try:
        import anthropic
    except ImportError:
        yield {"type": "error", "message": "anthropic package not installed. Run: pip install anthropic"}
        return

    from src.config_settings import get_settings
    s = get_settings()

    # Use Bedrock when: llm_backend=bedrock OR no direct Anthropic API key is configured.
    # AnthropicBedrock picks up AWS credentials from the environment (boto3 chain).
    if s.llm_backend == "bedrock" or not s.anthropic_api_key:
        client = anthropic.AnthropicBedrock(aws_region=s.bedrock_region)
        model = s.bedrock_model_id
    else:
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        model = getattr(s, "anthropic_model_id", "claude-3-5-sonnet-20241022")

    messages: list[dict] = [{"role": "user", "content": user_text}]
    max_iterations = 10

    for _ in range(max_iterations):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=_TOOLS_SCHEMA,
            messages=messages,
        )

        assistant_content: list[dict] = []
        for block in response.content:
            if hasattr(block, "text"):
                yield {"type": "token", "text": block.text}
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })

        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason != "tool_use":
            break

        tool_results: list[dict] = []
        stop_after_tools = False

        for block in response.content:
            if block.type != "tool_use":
                continue
            name = block.name
            inp = block.input

            yield {"type": "tool_start", "tool": name, "input": inp}

            try:
                if name == "search_nodes":
                    result = _search_nodes(inp["query"], inp.get("node_type"))

                elif name == "get_node_details":
                    result = _get_node_details(inp["node_id"])

                elif name == "get_node_relationships":
                    result = _get_node_relationships(inp["node_id"])

                elif name == "propose_changes":
                    change_id = str(uuid.uuid4())
                    # Emit the pending_change frame — WS handler captures it
                    yield {
                        "type": "pending_change",
                        "changeId": change_id,
                        "summary": inp.get("summary", ""),
                        "changes": inp.get("changes", []),
                    }
                    # Return a sentinel to the LLM so it knows to stop
                    result = {"awaiting_confirmation": True, "changeId": change_id}
                    stop_after_tools = True

                else:
                    result = {"error": f"Unknown tool: {name}"}

            except Exception as exc:
                result = {"error": str(exc)}

            yield {"type": "tool_end", "tool": name, "result": result}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })

        messages.append({"role": "user", "content": tool_results})

        # Stop the agent loop after propose_changes so the WS handler takes over
        if stop_after_tools:
            break

    yield {"type": "done"}
