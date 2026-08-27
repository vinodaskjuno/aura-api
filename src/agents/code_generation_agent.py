from __future__ import annotations
import json
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref


class CodeGenerationAgent(BaseAgent):
    name = "code_generation_agent"
    description = "Generate boilerplate, scaffolding, or new code based on requirements and project context"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("CodeGenerationAgent: Generating code from requirements")
        try:
            import boto3
            from src.config_settings import get_settings
            s = get_settings()
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)

            # Get prior code analysis for context
            prior_analysis = context.prior_results.get("code_analysis_agent")
            tech_context = ""
            if prior_analysis:
                tech_stack = prior_analysis.output.get("tech_stack", [])
                tech_context = f"Existing tech stack: {', '.join(tech_stack)}\n"

            prompt = f"""{tech_context}Generate production-ready code based on this request:
{context.intent}

Return JSON: {{"files": [{{"filename": "...", "language": "...", "content": "..."}}], "description": "..."}}"""

            body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}]}
            resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                       contentType="application/json", accept="application/json")
            text = json.loads(resp["body"].read())["content"][0]["text"]
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                result.output = data
                files = data.get("files", [])
                result.log(f"Generated {len(files)} code files")
                # Save each to S3
                from src.storage.s3_client import put_object
                for f in files:
                    key = f"generated/{context.project_id or 'default'}/{f['filename']}"
                    uri = put_object("analysis", key, f["content"], "text/plain")
                    result.artifacts.append(S3Ref(bucket="aura-analysis", key=key, uri=uri))
                    result.log(f"  Saved: {f['filename']}")
        except Exception as exc:
            result.output = {"files": [], "error": str(exc)}
            result.log(f"CodeGenerationAgent warning: {exc}")
        return result.finish("success")
