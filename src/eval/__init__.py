"""Evaluation Engine — LLM-as-judge scoring with dataset management."""

from src.eval.router import router
from src.eval.judge import EvalJudge, EvalResult, EvalRequest, eval_judge
from src.eval.datasets import DatasetStore, Dataset, dataset_store

__all__ = [
    "router",
    "EvalJudge", "EvalResult", "EvalRequest", "eval_judge",
    "DatasetStore", "Dataset", "dataset_store",
]
