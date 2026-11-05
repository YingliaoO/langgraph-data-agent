
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(..., description="User query string.")
    top_k: int = Field(default=10, ge=1, le=100, description="Number of results to return.")
    candidate_k: Optional[int] = Field(
        default=None, ge=1, le=100,
        description="Vector candidate budget before reranking; never smaller than top_k."
    )
    rerank: Optional[bool] = Field(
        default=None,
        description="None follows service configuration; false bypasses reranking for comparison."
    )
    retrieval_mode: Literal["dense", "hybrid"] = Field(default="dense", description="Hybrid fuses independent dense/lexical candidate ranks.")
    as_of: Optional[date] = Field(default=None, description="Explicit policy effective date; never guessed from query keywords.")
    threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional minimum similarity threshold.",
    )
    metric: str = Field(default="cosine", description="Similarity metric (cosine or l2).")
    company_filter: Optional[str] = Field(default=None, description="Optional company name filter.")
    source_tables: Optional[list[str]] = Field(default=None, description="Filter by source tables.")
    domain: Optional[str] = Field(default=None, description="Optional knowledge domain filter.")
    dataset_id: Optional[str] = Field(default=None, description="Optional stable dataset identity filter.")


class HybridSearchRequest(BaseModel):
    query: str = Field(..., description="User query string.")
    vector_top_k: int = Field(default=20, ge=1, le=100, description="Number of candidates from vector search.")
    rerank_top_k: int = Field(default=10, ge=1, le=100, description="Number of results after reranking.")
    threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional minimum similarity threshold.",
    )
    metric: str = Field(default="cosine", description="Similarity metric (cosine or l2).")
    source_tables: Optional[list[str]] = Field(default=None, description="Filter by source tables.")
    domain: Optional[str] = Field(default=None, description="Optional knowledge domain filter.")
    dataset_id: Optional[str] = Field(default=None, description="Optional stable dataset identity filter.")
