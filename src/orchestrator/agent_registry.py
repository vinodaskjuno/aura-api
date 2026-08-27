"""Agent registry — maps agent names to agent instances."""
from __future__ import annotations
from src.agents.base_agent import BaseAgent

_registry: dict[str, BaseAgent] = {}


def register(agent: BaseAgent) -> None:
    _registry[agent.name] = agent


def get_agent(name: str) -> BaseAgent | None:
    return _registry.get(name)


def all_agents() -> dict[str, BaseAgent]:
    return dict(_registry)


def bootstrap() -> None:
    """Import and register all agents — legacy + new AURA pipeline agents."""
    from src.agents.requirement_agent import RequirementAgent
    from src.agents.code_analysis_agent import CodeAnalysisAgent
    from src.agents.code_generation_agent import CodeGenerationAgent
    from src.agents.test_generation_agent import TestGenerationAgent
    from src.agents.test_execution_agent import TestExecutionAgent
    from src.agents.reverse_engineering_agent import ReverseEngineeringAgent
    from src.agents.security_agent import SecurityAgent
    from src.agents.vulnerability_remediation_agent import VulnerabilityRemediationAgent
    from src.agents.aiops_agent import AIOpsAgent
    from src.agents.rca_agent import RCAAgent
    from src.agents.deployment_agent import DeploymentAgent
    from src.agents.self_healing_agent import SelfHealingAgent
    from src.agents.knowledge_graph_agent import KnowledgeGraphAgent
    from src.agents.sop_agent import SOPAgent
    from src.agents.container_test_runner import ContainerTestRunnerAgent
    # AURA pipeline agents
    from src.agents.project_scanner_agent import ProjectScannerAgent
    from src.agents.file_analyzer_agent import FileAnalyzerAgent
    from src.agents.architecture_analyzer_agent import ArchitectureAnalyzerAgent
    from src.agents.domain_analyzer_agent import DomainAnalyzerAgent
    from src.agents.business_logic_analyzer_agent import BusinessLogicAnalyzerAgent
    from src.agents.data_flow_analyzer_agent import DataFlowAnalyzerAgent
    from src.agents.api_analyzer_agent import ApiAnalyzerAgent
    from src.agents.database_analyzer_agent import DatabaseAnalyzerAgent
    from src.agents.dependency_analyzer_agent import DependencyAnalyzerAgent
    from src.agents.infrastructure_analyzer_agent import InfrastructureAnalyzerAgent
    from src.agents.knowledge_analyzer_agent import KnowledgeAnalyzerAgent
    from src.agents.article_analyzer_agent import ArticleAnalyzerAgent
    from src.agents.graph_builder_agent import GraphBuilderAgent
    from src.agents.graph_reviewer_agent import GraphReviewerAgent
    from src.agents.knowledge_validator_agent import KnowledgeValidatorAgent
    from src.agents.tour_builder_agent import TourBuilderAgent
    from src.agents.teacher_agent import TeacherAgent
    from src.agents.impact_analyzer_agent import ImpactAnalyzerAgent
    from src.agents.remediation_agent import RemediationAgent

    for agent in [
        # Legacy agents
        RequirementAgent(), CodeAnalysisAgent(), CodeGenerationAgent(),
        TestGenerationAgent(), TestExecutionAgent(), ReverseEngineeringAgent(),
        SecurityAgent(), VulnerabilityRemediationAgent(), AIOpsAgent(),
        RCAAgent(), DeploymentAgent(), SelfHealingAgent(), KnowledgeGraphAgent(),
        SOPAgent(), ContainerTestRunnerAgent(),
        # AURA pipeline agents
        ProjectScannerAgent(), FileAnalyzerAgent(), ArchitectureAnalyzerAgent(),
        DomainAnalyzerAgent(), BusinessLogicAnalyzerAgent(), DataFlowAnalyzerAgent(),
        ApiAnalyzerAgent(), DatabaseAnalyzerAgent(), DependencyAnalyzerAgent(),
        InfrastructureAnalyzerAgent(), KnowledgeAnalyzerAgent(), ArticleAnalyzerAgent(),
        GraphBuilderAgent(), GraphReviewerAgent(), KnowledgeValidatorAgent(),
        TourBuilderAgent(), TeacherAgent(), ImpactAnalyzerAgent(), RemediationAgent(),
    ]:
        register(agent)
