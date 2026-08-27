from __future__ import annotations
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult


class RequirementAgent(BaseAgent):
    name = "requirement_agent"
    description = "Extract and structure requirements from user descriptions, Jira stories, or documents"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("RequirementAgent: Extracting requirements from intent")
        try:
            import boto3, json
            from src.config_settings import get_settings
            s = get_settings()
            client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
            prompt = f"""Extract structured requirements from this user input.
Return JSON with: {{"requirements": [{{"id": "REQ-1", "title": "...", "description": "...", "priority": "high|medium|low", "type": "functional|non-functional"}}]}}

User input: {context.intent}"""
            body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}]}
            resp = client.invoke_model(modelId=s.bedrock_model_id, body=json.dumps(body),
                                       contentType="application/json", accept="application/json")
            text = json.loads(resp["body"].read())["content"][0]["text"]
            # Extract JSON from response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
                result.output = data
                reqs = data.get("requirements", [])
                result.log(f"Extracted {len(reqs)} requirements")
                for req in reqs:
                    result.log(f"  [{req.get('priority','?').upper()}] {req.get('id','')}: {req.get('title','')}")
        except Exception as exc:
            result.output = {"requirements": [], "error": str(exc)}
            result.log(f"RequirementAgent warning: {exc}")
        return result.finish("success")
