import hashlib
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class Evidence(BaseModel):

    evidence_id: str | None = None

    kind: str

    content: Any

    source: str | None = None

    # Stable retrieval identity and ordering. These fields are optional so
    # legacy adapters can continue to emit the original contract while the
    # canonical tools progressively provide richer evidence metadata.
    source_id: str | None = None

    rank: int | None = None

    score: float | None = None

    retrieval_score: float | None = None

    rerank_score: float | None = None

    citation: str | None = None

    task_id: str | None = None

    metadata: dict = Field(default_factory=dict)



class Statement(BaseModel):

    language: Literal["sql", "cypher"]

    text: str

    parameters: dict = Field(default_factory=dict)



class ToolResult(BaseModel):

    tool_name: str
    tool_kind: str | None = None

    status: Literal[
        "success",
        "empty",
        "error"
    ]

    task: str | None = None

    statement: Statement | None = None

    data: list[dict] | None = None

    answer: str | None = None

    evidence: list[Evidence] = Field(
        default_factory=list
    )

    evidence_ids: list[str] = Field(default_factory=list)

    metadata: dict = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def assign_deterministic_evidence_ids(self) -> "ToolResult":
        """Give direct tool callers the same stable IDs as HTTP serialization."""
        if not self.evidence:
            self.evidence_ids = []
            return self
        digest = hashlib.sha256(f"{self.tool_name}:{self.task or ''}".encode("utf-8")).hexdigest()[:10]
        task_id = f"task_{digest}"
        ids: list[str] = []
        for index, item in enumerate(self.evidence, start=1):
            evidence_id = item.evidence_id or f"ev_{task_id}_{index}"
            item.evidence_id = evidence_id
            item.task_id = item.task_id or task_id
            ids.append(evidence_id)
        self.evidence_ids = ids
        return self
