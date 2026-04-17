"""Pydantic models for Evaluation Engine API responses.

Defines the request/response schemas used by the evaluation router
for LLM-as-judge scoring, batch evaluations, dataset management,
and statistics.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Score / Result models
# ---------------------------------------------------------------------------


class EvalScoreResponse(BaseModel):
    """Score for a single evaluation criterion."""

    criterion: str
    score: float
    reasoning: str
    model_used: str
    latency_ms: float


class EvalResultResponse(BaseModel):
    """Complete evaluation result for a single input/output pair."""

    request_id: str
    scores: List[EvalScoreResponse]
    overall_score: float
    created_at: float
    input_text: str
    output_text: str


class EvalRunResponse(BaseModel):
    """Response wrapper for a single evaluation run."""

    result: EvalResultResponse


class EvalResultListResponse(BaseModel):
    """Response for listing evaluation results."""

    count: int
    results: List[EvalResultResponse]


# ---------------------------------------------------------------------------
# Batch evaluation models
# ---------------------------------------------------------------------------


class BatchEvalEntryResult(BaseModel):
    """Result for a single entry in a batch evaluation."""

    status: str  # "success" or "error"
    result: Optional[EvalResultResponse] = None
    error: Optional[str] = None
    input_preview: Optional[str] = None


class BatchEvalResponse(BaseModel):
    """Response for a batch evaluation run."""

    dataset_id: str
    total_entries: int
    successful: int
    failed: int
    average_score: float
    results: List[BatchEvalEntryResult]


# ---------------------------------------------------------------------------
# Statistics models
# ---------------------------------------------------------------------------


class ScoreDistribution(BaseModel):
    """Distribution of scores across quality buckets."""

    excellent: int = 0  # >= 0.75
    good: int = 0       # >= 0.5
    average: int = 0    # >= 0.25
    poor: int = 0       # < 0.25


class EvalStatsResponse(BaseModel):
    """Aggregated evaluation statistics."""

    total_evaluations: int
    average_overall_score: float
    criteria_averages: Dict[str, float]
    score_distribution: ScoreDistribution
    latest_evaluation: Optional[float] = None


class EvalStatsWrapper(BaseModel):
    """Response wrapper for evaluation statistics."""

    stats: EvalStatsResponse


# ---------------------------------------------------------------------------
# Dataset models
# ---------------------------------------------------------------------------


class DatasetEntryResponse(BaseModel):
    """A single entry in an evaluation dataset."""

    input_text: str
    expected_output: Optional[str] = None
    reference: Optional[str] = None
    metadata: Dict[str, Any] = {}


class DatasetResponse(BaseModel):
    """Full dataset response with all entries."""

    dataset_id: str
    name: str
    description: str = ""
    entries: List[DatasetEntryResponse] = []
    created_at: float
    updated_at: float
    tags: List[str] = []


class DatasetSummary(BaseModel):
    """Abbreviated dataset info for list views."""

    dataset_id: str
    name: str
    description: str = ""
    entry_count: int
    tags: List[str] = []
    created_at: float
    updated_at: float


class DatasetCreatedResponse(BaseModel):
    """Response after creating a dataset."""

    status: str = "created"
    dataset: DatasetResponse


class DatasetListResponse(BaseModel):
    """Response for listing datasets."""

    count: int
    datasets: List[DatasetSummary]


class DatasetDeletedResponse(BaseModel):
    """Response after deleting a dataset."""

    status: str = "deleted"
    dataset_id: str


class DatasetEntriesAddedResponse(BaseModel):
    """Response after adding entries to a dataset."""

    status: str = "added"
    dataset_id: str
    added: int
    total_entries: int
