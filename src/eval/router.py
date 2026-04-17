"""FastAPI router for the Evaluation Engine API.

Provides endpoints for LLM-as-judge evaluation, batch evaluation
on datasets, result retrieval, and dataset CRUD operations.

Endpoints:
  POST   /eval/run                   -- Run evaluation on a single input/output pair
  POST   /eval/batch                 -- Run batch evaluation on a dataset
  GET    /eval/results               -- Get evaluation results (with filters)
  GET    /eval/stats                 -- Get evaluation statistics
  POST   /eval/datasets              -- Create a dataset
  GET    /eval/datasets              -- List datasets
  GET    /eval/datasets/{id}         -- Get a dataset
  POST   /eval/datasets/{id}/entries -- Add entries to a dataset
  DELETE /eval/datasets/{id}         -- Delete a dataset
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.eval.datasets import dataset_store
from src.eval.judge import EvalCriterion, EvalJudge, EvalRequest, eval_judge

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Evaluation"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class RunEvalRequest(BaseModel):
    """Request body for a single evaluation run."""

    input_text: str
    output_text: str
    reference_text: Optional[str] = None
    criteria: List[EvalCriterion] = [EvalCriterion.RELEVANCE, EvalCriterion.HELPFULNESS]
    judge_model: str = "gpt-4o-mini"
    judge_provider: str = "openai"
    custom_criterion_name: Optional[str] = None
    custom_criterion_description: Optional[str] = None


class BatchEvalRequest(BaseModel):
    """Request body for batch evaluation on a dataset."""

    dataset_id: str
    output_texts: Optional[List[str]] = None  # If None, use dataset expected_outputs
    criteria: List[EvalCriterion] = [EvalCriterion.RELEVANCE, EvalCriterion.HELPFULNESS]
    judge_model: str = "gpt-4o-mini"
    judge_provider: str = "openai"
    max_concurrent: int = 5


class CreateDatasetRequest(BaseModel):
    """Request body for creating an evaluation dataset."""

    name: str
    description: str = ""
    entries: Optional[List[Dict[str, Any]]] = None
    tags: Optional[List[str]] = None


class AddEntriesRequest(BaseModel):
    """Request body for adding entries to a dataset."""

    entries: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Evaluation routes
# ---------------------------------------------------------------------------


@router.post("/run")
async def run_evaluation(request: RunEvalRequest) -> Dict[str, Any]:
    """Run LLM-as-judge evaluation on a single input/output pair.

    Evaluates the output against each specified criterion by sending
    structured rubric prompts to the judge model via the gateway.
    """
    eval_request = EvalRequest(
        input_text=request.input_text,
        output_text=request.output_text,
        reference_text=request.reference_text,
        criteria=request.criteria,
        judge_model=request.judge_model,
        judge_provider=request.judge_provider,
        custom_criterion_name=request.custom_criterion_name,
        custom_criterion_description=request.custom_criterion_description,
    )

    result = await eval_judge.evaluate(eval_request)

    logger.info(
        "evaluation_run",
        request_id=result.request_id,
        overall_score=result.overall_score,
    )

    return {"result": result.model_dump()}


@router.post("/batch")
async def run_batch_evaluation(request: BatchEvalRequest) -> Dict[str, Any]:
    """Run batch evaluation on a dataset.

    Evaluates each entry in the dataset, optionally using provided
    output texts. Runs evaluations with controlled concurrency.
    """
    dataset = dataset_store.get(request.dataset_id)
    if not dataset:
        raise HTTPException(
            status_code=404, detail=f"Dataset not found: {request.dataset_id}"
        )

    if not dataset.entries:
        raise HTTPException(
            status_code=400, detail="Dataset has no entries"
        )

    # Determine outputs
    if request.output_texts:
        if len(request.output_texts) != len(dataset.entries):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"output_texts length ({len(request.output_texts)}) must match "
                    f"dataset entries ({len(dataset.entries)})"
                ),
            )
        outputs = request.output_texts
    else:
        # Use expected outputs from dataset
        outputs = []
        for entry in dataset.entries:
            if entry.expected_output:
                outputs.append(entry.expected_output)
            else:
                outputs.append("")

    # Build evaluation requests
    eval_requests: list[EvalRequest] = []
    for entry, output in zip(dataset.entries, outputs):
        if not output:
            continue  # Skip entries without output
        eval_requests.append(EvalRequest(
            input_text=entry.input_text,
            output_text=output,
            reference_text=entry.reference,
            criteria=request.criteria,
            judge_model=request.judge_model,
            judge_provider=request.judge_provider,
        ))

    if not eval_requests:
        raise HTTPException(
            status_code=400,
            detail="No valid entries to evaluate (all outputs are empty)",
        )

    # Run with controlled concurrency
    semaphore = asyncio.Semaphore(request.max_concurrent)

    async def _eval_with_semaphore(req: EvalRequest) -> Dict[str, Any]:
        async with semaphore:
            try:
                result = await eval_judge.evaluate(req)
                return {"status": "success", "result": result.model_dump()}
            except Exception as exc:
                logger.error("batch_eval_entry_failed", error=str(exc))
                return {
                    "status": "error",
                    "error": str(exc),
                    "input_preview": req.input_text[:100],
                }

    tasks = [_eval_with_semaphore(req) for req in eval_requests]
    batch_results = await asyncio.gather(*tasks)

    # Compute batch statistics
    successful = [r for r in batch_results if r["status"] == "success"]
    failed = [r for r in batch_results if r["status"] == "error"]

    avg_score = 0.0
    if successful:
        scores = [r["result"]["overall_score"] for r in successful]
        avg_score = sum(scores) / len(scores)

    logger.info(
        "batch_evaluation_completed",
        dataset_id=request.dataset_id,
        total=len(eval_requests),
        successful=len(successful),
        failed=len(failed),
        avg_score=round(avg_score, 4),
    )

    return {
        "dataset_id": request.dataset_id,
        "total_entries": len(eval_requests),
        "successful": len(successful),
        "failed": len(failed),
        "average_score": round(avg_score, 4),
        "results": batch_results,
    }


@router.get("/results")
async def get_results(
    limit: int = Query(100, ge=1, le=1000, description="Max results to return"),
    min_score: Optional[float] = Query(None, ge=0.0, le=1.0, description="Minimum overall score filter"),
    criterion: Optional[str] = Query(None, description="Filter by criterion name"),
) -> Dict[str, Any]:
    """Get evaluation results with optional filters."""
    results = eval_judge.get_results(limit=limit)

    # Apply filters
    if min_score is not None:
        results = [r for r in results if r.overall_score >= min_score]

    if criterion:
        results = [
            r for r in results
            if any(s.criterion == criterion for s in r.scores)
        ]

    return {
        "count": len(results),
        "results": [r.model_dump() for r in results],
    }


@router.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """Get aggregated evaluation statistics."""
    return {"stats": eval_judge.get_stats()}


# ---------------------------------------------------------------------------
# Dataset routes
# ---------------------------------------------------------------------------


@router.post("/datasets", status_code=201)
async def create_dataset(request: CreateDatasetRequest) -> Dict[str, Any]:
    """Create a new evaluation dataset."""
    dataset = dataset_store.create(
        name=request.name,
        entries=request.entries,
        description=request.description,
        tags=request.tags,
    )

    logger.info(
        "dataset_created",
        dataset_id=dataset.dataset_id,
        name=dataset.name,
        entry_count=len(dataset.entries),
    )

    return {"status": "created", "dataset": dataset.model_dump()}


@router.get("/datasets")
async def list_datasets() -> Dict[str, Any]:
    """List all evaluation datasets."""
    datasets = dataset_store.list_all()
    return {
        "count": len(datasets),
        "datasets": [
            {
                "dataset_id": d.dataset_id,
                "name": d.name,
                "description": d.description,
                "entry_count": len(d.entries),
                "tags": d.tags,
                "created_at": d.created_at,
                "updated_at": d.updated_at,
            }
            for d in datasets
        ],
    }


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str) -> Dict[str, Any]:
    """Get a dataset by ID, including all entries."""
    dataset = dataset_store.get(dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail=f"Dataset not found: {dataset_id}")
    return {"dataset": dataset.model_dump()}


@router.post("/datasets/{dataset_id}/entries")
async def add_entries(dataset_id: str, request: AddEntriesRequest) -> Dict[str, Any]:
    """Add entries to an existing dataset."""
    try:
        dataset = dataset_store.add_entries(
            dataset_id=dataset_id,
            entries=request.entries,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Dataset not found: {dataset_id}")

    logger.info(
        "dataset_entries_added",
        dataset_id=dataset_id,
        added=len(request.entries),
        total=len(dataset.entries),
    )

    return {
        "status": "added",
        "dataset_id": dataset_id,
        "added": len(request.entries),
        "total_entries": len(dataset.entries),
    }


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str) -> Dict[str, Any]:
    """Delete an evaluation dataset."""
    deleted = dataset_store.delete(dataset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Dataset not found: {dataset_id}")

    logger.info("dataset_deleted", dataset_id=dataset_id)
    return {"status": "deleted", "dataset_id": dataset_id}
