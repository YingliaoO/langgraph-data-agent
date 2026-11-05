from langchain_core.prompts import ChatPromptTemplate

from multisource_data_agent.application.agents.kg_sub_graph.prompts.kg_prompts import TOOL_SELECTION_SYSTEM_PROMPT


def create_tool_selection_prompt_template() -> ChatPromptTemplate:
    """Create the canonical semantic tool-selection prompt."""

    message = """Sub-task: {question}
Choose exactly one canonical data tool for this self-contained evidence task."""

    return ChatPromptTemplate.from_messages(
        [
            ("system", TOOL_SELECTION_SYSTEM_PROMPT),
            ("human", message),
        ]
    )
