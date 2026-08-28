"""Intent classifier — maps user intent to a list of agent names to invoke."""
from __future__ import annotations
import logging
import json
import boto3
from src.config_settings import get_settings

logger = logging.getLogger(__name__)

CLASSIFICATION_PROMPT = """You are an intent classifier for the AURA AI Dev Agent Platform.
Given a user message, return a JSON object with:
  - "agents": list of agent names to invoke (ordered by execution priority)
  - "intent_summary": one-line description of what the user wants

Available agents:
  requirement_agent         - extract/manage requirements from descriptions
  code_analysis_agent       - analyse source code (Java, Python, TS, Mule, Cobol, Angular, React)
  code_generation_agent     - generate boilerplate, scaffolding, or new code
  test_generation_agent     - generate Playwright, API, integration, regression, boundary tests
  test_execution_agent      - execute test suites and report results
  reverse_engineering_agent - reverse-engineer legacy code into knowledge graph
  security_agent            - scan for vulnerabilities, CVE lookup
  vulnerability_remediation_agent - generate fixes for security findings
  aiops_agent               - ingest CloudWatch/Prometheus/Splunk monitoring data
  rca_agent                 - perform root cause analysis on incidents
  deployment_agent          - trigger CI/CD pipelines and Kubernetes deployments
  self_healing_agent        - auto-remediate Kubernetes pod failures
  knowledge_graph_agent     - update and query the Neptune knowledge graph
  obs_signal_collector      - collect logs/metrics/traces/deploys from Grafana, Datadog, Sentry, Elasticsearch, CloudWatch, Kubernetes
  obs_correlator            - correlate signals into a timeline and align deploys to change points
  obs_case_retrieval        - retrieve similar past incidents as priors
  obs_runbook               - match a runbook and mark steps the evidence satisfies
  obs_hypothesis            - generate competing root-cause hypotheses
  obs_root_cause            - produce an evidence-cited root cause
  obs_verifier              - re-check signals after a fix window

Rules:
- Always include knowledge_graph_agent LAST if any other agent produces output
- For code analysis requests: code_analysis_agent + reverse_engineering_agent + knowledge_graph_agent
- For test requests: test_generation_agent (and test_execution_agent if "run" is mentioned)
- For security requests: security_agent + vulnerability_remediation_agent + knowledge_graph_agent
- For incident/alert/ops: aiops_agent + rca_agent + knowledge_graph_agent
- For a full SRE investigation of a live service (observability, Grafana/Loki/Datadog/
  Sentry, "investigate", "root cause with evidence", postmortem): obs_signal_collector +
  obs_correlator + obs_case_retrieval + obs_runbook + obs_hypothesis + obs_root_cause
- Return ONLY valid JSON, no extra text.

Example output:
{"agents": ["code_analysis_agent", "knowledge_graph_agent"], "intent_summary": "Analyse Java codebase"}
"""


def classify(user_message: str) -> dict:
    """Call Bedrock Claude to classify intent. Falls back to keyword matching."""
    try:
        return _bedrock_classify(user_message)
    except Exception as exc:
        logger.warning("Bedrock classify failed, using keyword fallback: %s", exc)
        return _keyword_classify(user_message)


def _bedrock_classify(user_message: str) -> dict:
    s = get_settings()
    client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "system": CLASSIFICATION_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    }
    resp = client.invoke_model(
        modelId=s.bedrock_model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    text = json.loads(resp["body"].read())["content"][0]["text"]
    return json.loads(text)


def _keyword_classify(user_message: str) -> dict:
    """Lightweight keyword-based fallback."""
    msg = user_message.lower()
    agents = []

    if any(w in msg for w in ["analyse", "analyze", "code", "repo", "repository", "java", "python", "mule", "angular", "react"]):
        agents += ["code_analysis_agent", "reverse_engineering_agent"]
    if any(w in msg for w in ["test", "playwright", "api test", "integration", "regression", "qa"]):
        agents += ["test_generation_agent"]
    if any(w in msg for w in ["run test", "execute test", "test run"]):
        agents += ["test_execution_agent"]
    if any(w in msg for w in ["security", "vulnerability", "cve", "scan", "veracode", "sonar"]):
        agents += ["security_agent", "vulnerability_remediation_agent"]
    if any(w in msg for w in ["alert", "incident", "monitor", "ops", "cloudwatch", "prometheus"]):
        agents += ["aiops_agent"]
    if any(w in msg for w in ["root cause", "rca", "why", "failure", "outage"]):
        agents += ["rca_agent"]
    if any(w in msg for w in ["investigate", "observability", "sre", "postmortem",
                              "runbook", "loki", "grafana", "datadog", "sentry",
                              "trace", "span", "evidence", "correlate"]):
        agents += ["obs_signal_collector", "obs_correlator", "obs_case_retrieval",
                   "obs_runbook", "obs_hypothesis", "obs_root_cause"]
    if any(w in msg for w in ["deploy", "release", "pipeline", "ci/cd", "kubernetes", "k8s"]):
        agents += ["deployment_agent"]
    if any(w in msg for w in ["requirement", "story", "feature", "jira"]):
        agents += ["requirement_agent"]
    if any(w in msg for w in ["generate", "scaffold", "create code", "boilerplate"]):
        agents += ["code_generation_agent"]
    if not agents:
        agents = ["knowledge_graph_agent"]
    elif "knowledge_graph_agent" not in agents:
        agents.append("knowledge_graph_agent")

    return {
        "agents": list(dict.fromkeys(agents)),  # deduplicate preserving order
        "intent_summary": f"Keyword-classified: {user_message[:80]}",
    }
