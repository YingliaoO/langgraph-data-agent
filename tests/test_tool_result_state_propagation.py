"""Canonical ToolResult state propagation checks."""

from multisource_data_agent.application.agents.kg_sub_graph.agentic_rag_agents.components.state import OutputState, OverallState
from multisource_data_agent.application.agents.lg_states import AgentState


def test_execution_fields_exist_at_each_state_boundary() -> None:
    assert "tool_results" in OverallState.__annotations__
    assert "evidence" in OverallState.__annotations__
    assert "citation_ids" in OverallState.__annotations__
    assert "tool_results" in OutputState.__annotations__
    assert "evidence" in OutputState.__annotations__
    assert "citation_ids" in OutputState.__annotations__
    assert "tasks" in OutputState.__annotations__
    assert "execution_summary" in OutputState.__annotations__
    assert "tool_results" in AgentState.__annotations__
    assert "evidence" in AgentState.__annotations__
    assert "citation_ids" in AgentState.__annotations__
    assert "tasks" in AgentState.__annotations__
    assert "execution_summary" in AgentState.__annotations__
