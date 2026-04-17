"""Evaluation dataset management with persistent storage.

Provides CRUD operations for evaluation datasets containing input/output
pairs used for batch evaluation of LLM quality.
"""

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DatasetEntry(BaseModel):
    """A single evaluation data point."""

    input_text: str
    expected_output: Optional[str] = None
    reference: Optional[str] = None
    metadata: dict = {}


class Dataset(BaseModel):
    """A named collection of evaluation entries."""

    dataset_id: str
    name: str
    description: str = ""
    entries: list[DatasetEntry] = []
    created_at: float
    updated_at: float
    tags: list[str] = []


# ---------------------------------------------------------------------------
# Dataset Store
# ---------------------------------------------------------------------------


class DatasetStore:
    """Persistent dataset store for evaluation.

    Datasets are stored in a JSON file on disk. All mutations are
    thread-safe and immediately flushed to disk via atomic rename.
    """

    def __init__(self, storage_path: str = ".data/datasets.json"):
        self._datasets: dict[str, Dataset] = {}
        self._storage_path = Path(storage_path)
        self._lock = threading.Lock()
        self._load()

    # -- Public API ----------------------------------------------------------

    def create(
        self,
        name: str,
        entries: Optional[list[dict]] = None,
        description: str = "",
        tags: Optional[list[str]] = None,
    ) -> Dataset:
        """Create a new evaluation dataset.

        Args:
            name: Human-readable dataset name.
            entries: Optional initial entries as list of dicts.
            description: Optional description.
            tags: Classification tags.

        Returns:
            The newly created ``Dataset``.
        """
        now = time.time()
        dataset_id = str(uuid.uuid4())

        parsed_entries: list[DatasetEntry] = []
        if entries:
            for entry_data in entries:
                parsed_entries.append(DatasetEntry(**entry_data))

        dataset = Dataset(
            dataset_id=dataset_id,
            name=name,
            description=description,
            entries=parsed_entries,
            created_at=now,
            updated_at=now,
            tags=tags or [],
        )

        with self._lock:
            self._datasets[dataset_id] = dataset
            self._save()

        logger.info(
            "dataset_created",
            dataset_id=dataset_id,
            name=name,
            entry_count=len(parsed_entries),
        )
        return dataset

    def add_entries(self, dataset_id: str, entries: list[dict]) -> Dataset:
        """Add new entries to an existing dataset.

        Args:
            dataset_id: The dataset to add entries to.
            entries: List of entry dicts with at least ``input_text``.

        Returns:
            The updated ``Dataset``.

        Raises:
            KeyError: If *dataset_id* does not exist.
        """
        with self._lock:
            if dataset_id not in self._datasets:
                raise KeyError(f"Dataset not found: {dataset_id}")

            dataset = self._datasets[dataset_id]
            new_entries = [DatasetEntry(**e) for e in entries]
            dataset.entries.extend(new_entries)
            dataset.updated_at = time.time()
            self._save()

        logger.info(
            "dataset_entries_added",
            dataset_id=dataset_id,
            added=len(new_entries),
            total=len(dataset.entries),
        )
        return dataset

    def get(self, dataset_id: str) -> Optional[Dataset]:
        """Get a dataset by ID, or ``None`` if not found."""
        with self._lock:
            return self._datasets.get(dataset_id)

    def list_all(self) -> list[Dataset]:
        """List all datasets sorted by updated_at descending."""
        with self._lock:
            datasets = list(self._datasets.values())
        datasets.sort(key=lambda d: d.updated_at, reverse=True)
        return datasets

    def delete(self, dataset_id: str) -> bool:
        """Delete a dataset.

        Returns:
            ``True`` if the dataset was deleted, ``False`` if not found.
        """
        with self._lock:
            if dataset_id not in self._datasets:
                return False
            del self._datasets[dataset_id]
            self._save()

        logger.info("dataset_deleted", dataset_id=dataset_id)
        return True

    # -- Persistence ---------------------------------------------------------

    def _save(self) -> None:
        """Flush current state to disk. Caller must hold ``_lock``."""
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            did: d.model_dump() for did, d in self._datasets.items()
        }
        tmp_path = self._storage_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, default=str))
        tmp_path.replace(self._storage_path)

    def _load(self) -> None:
        """Load state from disk if the storage file exists."""
        if not self._storage_path.exists():
            return
        try:
            raw = json.loads(self._storage_path.read_text())
            for did, ddata in raw.items():
                self._datasets[did] = Dataset(**ddata)
            logger.info("datasets_loaded", count=len(self._datasets))
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("dataset_store_load_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

dataset_store = DatasetStore()
