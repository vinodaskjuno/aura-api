"""SOPAgent — generates Standard Operating Procedures using AWS Bedrock Claude.

Reads project knowledge graph, analysis results, and stage context to produce
a structured, step-by-step SOP document specific to that project.
Falls back to template steps when Bedrock is unavailable.
"""
from __future__ import annotations
import json
import uuid
import logging
from datetime import datetime, timezone

from src.agents.base_agent import BaseAgent, AgentContext, AgentResult
from src.models.sop import SOPDocument, SOPStep, SOPChecklistItem, TEMPLATE_STEPS, STAGE_META

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert software engineering process consultant generating a Standard Operating Procedure (SOP).
Given project context (tech stack, services, correlations, infrastructure), generate a precise, actionable SOP
for the specified stage. Each step should reference the actual project artifacts where possible.

Return ONLY valid JSON in this exact format:
{
  "title": "SOP title",
  "summary": "One sentence describing this SOP",
  "steps": [
    {
      "title": "Step title",
      "description": "What to do and why",
      "checklist": ["Checklist item 1", "Checklist item 2", "Checklist item 3"]
    }
  ]
}

Rules:
- Include 4-8 steps appropriate to the stage
- Make checklist items specific to the actual project (use service names, tech stack, etc.)
- Keep each step title under 40 characters
- Keep descriptions under 120 characters
- 2-4 checklist items per step
- Return ONLY JSON, no extra text
"""


class SOPAgent(BaseAgent):
    name = "sop_agent"
    description = "Generates AI-powered Standard Operating Procedures from project knowledge graph"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        stage = context.extra.get("stage", "dev")
        project_id = context.project_id or "unknown"
        project_name = context.extra.get("project_name", "Project")

        result.log(f"SOPAgent: Generating {stage} SOP for {project_name}")

        # Gather project context from prior agent results or KG
        kg_context = self._build_kg_context(context)

        # Try Bedrock generation
        sop_data = None
        try:
            sop_data = await self._generate_with_bedrock(stage, project_name, kg_context, result)
        except Exception as exc:
            result.log(f"Bedrock unavailable — using template: {exc}")

        # Fall back to templates
        if not sop_data:
            sop_data = self._build_from_template(stage, project_name, kg_context)
            result.log("Using template-based SOP (Bedrock not available)")

        # Build SOPDocument
        sop = self._build_sop_document(project_id, stage, project_name, sop_data, kg_context)

        # Persist to DynamoDB
        try:
            from src.database.dynamo_client import put_item
            import json as _json
            put_item("sops", {
                "sopId":      sop.sopId,
                "projectId":  sop.projectId,
                "stage":      sop.stage,
                "title":      sop.title,
                "summary":    sop.summary,
                "steps":      _json.dumps([s.model_dump() for s in sop.steps]),
                "status":     sop.status,
                "approvedBy": sop.approvedBy,
                "approvedAt": sop.approvedAt,
                "generatedAt":sop.generatedAt,
            })
            result.log(f"SOP saved: {sop.sopId} ({len(sop.steps)} steps)")
        except Exception as exc:
            result.log(f"DynamoDB save warning: {exc}")

        result.output = sop.model_dump()
        return result.finish("success")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_kg_context(self, context: AgentContext) -> dict:
        """Extract project knowledge from prior agent results or context extra."""
        kg = context.extra.get("kg", {})
        code = kg.get("code", {})
        return {
            "tech_stack":   code.get("tech_stack", [])[:10],
            "services":     code.get("services", [])[:10],
            "languages":    code.get("languages", [])[:6],
            "infra":        [i.get("name", i) if isinstance(i, dict) else i for i in kg.get("infra", [])][:6],
            "db_servers":   [d.get("name", d) if isinstance(d, dict) else d for d in kg.get("db_servers", [])][:6],
            "correlations": kg.get("correlations", [])[:10],
        }

    async def _generate_with_bedrock(self, stage: str, project_name: str,
                                      kg: dict, result: AgentResult) -> dict | None:
        import boto3
        from src.config_settings import get_settings
        import asyncio

        s = get_settings()

        user_prompt = f"""Generate a {STAGE_META[stage]['label']} SOP for the project: {project_name}

Tech Stack: {', '.join(kg['tech_stack']) or 'Not analysed yet'}
Services: {', '.join(kg['services']) or 'Not analysed yet'}
Languages: {', '.join(kg['languages']) or 'Not detected yet'}
Infrastructure: {', '.join(kg['infra']) or 'None detected'}
Databases: {', '.join(kg['db_servers']) or 'None detected'}
Key Relationships: {len(kg['correlations'])} service correlations mapped

Stage: {stage}
Generate a precise, project-specific SOP for this stage."""

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.invoke_model(
                modelId=s.bedrock_model_id,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            ),
        )
        text = json.loads(resp["body"].read())["content"][0]["text"].strip()

        # Extract JSON
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        data = json.loads(text[start:end])
        result.log(f"Bedrock generated {len(data.get('steps', []))} steps")
        return data

    def _build_from_template(self, stage: str, project_name: str, kg: dict) -> dict:
        """Build SOP from template, injecting real project data where possible."""
        templates = TEMPLATE_STEPS.get(stage, TEMPLATE_STEPS["dev"])
        steps = []
        for t in templates:
            # Inject real project context into checklist items
            checklist = list(t["checklist"])
            if stage == "dev" and t["title"] == "Code Analysis":
                if kg["tech_stack"]:
                    checklist.append(f"Tech stack confirmed: {', '.join(kg['tech_stack'][:4])}")
                if kg["services"]:
                    checklist.append(f"Services verified: {', '.join(kg['services'][:3])}")
            elif stage == "aiops" and t["title"] == "Root Cause Analysis":
                if kg["services"]:
                    checklist.append(f"Check affected services: {', '.join(kg['services'][:3])}")
            steps.append({"title": t["title"], "description": t["description"], "checklist": checklist})

        meta = STAGE_META[stage]
        return {
            "title":   f"{meta['label']} SOP — {project_name}",
            "summary": f"Standard operating procedure for the {meta['label']} stage of {project_name}.",
            "steps":   steps,
        }

    def _build_sop_document(self, project_id: str, stage: str, project_name: str,
                             sop_data: dict, kg: dict) -> SOPDocument:
        now = datetime.now(timezone.utc).isoformat()
        steps = []
        for i, s in enumerate(sop_data.get("steps", [])):
            step_id = str(uuid.uuid4())[:8]
            checklist = [
                SOPChecklistItem(id=str(uuid.uuid4())[:8], text=item)
                for item in s.get("checklist", [])
            ]
            # Auto-detect first 2 steps for dev if KG data present
            auto = (stage == "dev" and i < 2 and (bool(kg["tech_stack"]) or bool(kg["services"])))
            steps.append(SOPStep(
                id=step_id, order=i + 1,
                title=s.get("title", f"Step {i+1}"),
                description=s.get("description", ""),
                checklist=checklist,
                status="completed" if (auto and i == 0) else "pending",
                autoDetected=auto,
            ))

        return SOPDocument(
            sopId=str(uuid.uuid4()),
            projectId=project_id,
            stage=stage,
            title=sop_data.get("title", f"{STAGE_META[stage]['label']} SOP — {project_name}"),
            summary=sop_data.get("summary", ""),
            steps=steps,
            status="draft",
            generatedAt=now,
        )
