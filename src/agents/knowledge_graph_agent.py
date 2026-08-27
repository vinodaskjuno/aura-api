from __future__ import annotations
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult


class KnowledgeGraphAgent(BaseAgent):
    name = "knowledge_graph_agent"
    description = "Update Amazon Neptune knowledge graph with new facts from all prior agents in the pipeline"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("KnowledgeGraphAgent: Collecting triples from all prior agents")

        all_triples = []
        for agent_name, prior_result in context.prior_results.items():
            if prior_result.kg_updates:
                result.log(f"  {agent_name}: {len(prior_result.kg_updates)} triples")
                all_triples.extend(prior_result.kg_updates)

        if not all_triples:
            result.log("No new triples to insert")
            return result.finish("success")

        result.log(f"Collected {len(all_triples)} triples (graph store not active)")
        result.output = {"triples_inserted": 0, "total_triples": 0}

        return result.finish("success")
