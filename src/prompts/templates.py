"""Prompt template engine with variable interpolation, versioning, and A/B testing.

Provides a persistent prompt store that supports:
  - Template CRUD with auto-detected variables from {{variable}} patterns
  - Version history for every content update
  - A/B testing via named variants
  - Thread-safe file-backed persistence
"""

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PromptTemplate(BaseModel):
    """A prompt template with variable placeholders and optional A/B variants."""

    template_id: str
    name: str
    description: str = ""
    content: str  # Template text with {{variable}} placeholders
    model: Optional[str] = None  # Recommended model
    provider: Optional[str] = None
    variables: list[str] = []  # Expected variable names
    tags: list[str] = []
    version: int = 1
    created_at: float
    updated_at: float
    created_by: str = "system"
    # A/B testing
    variants: dict[str, str] = {}  # variant_name -> template content
    active_variant: Optional[str] = None  # None = use main content


class PromptVersion(BaseModel):
    """A snapshot of a template at a specific version."""

    version: int
    content: str
    created_at: float
    created_by: str
    change_note: str = ""


# ---------------------------------------------------------------------------
# Variable extraction
# ---------------------------------------------------------------------------

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def _extract_variables(content: str) -> list[str]:
    """Extract unique variable names from {{variable}} patterns in content."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _VAR_PATTERN.finditer(content):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


# ---------------------------------------------------------------------------
# Prompt Store
# ---------------------------------------------------------------------------


class PromptStore:
    """Persistent prompt template store with versioning and thread safety.

    Templates are stored in a JSON file on disk. Every mutation acquires
    the internal lock and flushes to disk before returning.
    """

    def __init__(self, storage_path: str = ".data/prompts.json"):
        self._templates: dict[str, PromptTemplate] = {}
        self._versions: dict[str, list[PromptVersion]] = {}  # template_id -> versions
        self._storage_path = Path(storage_path)
        self._lock = threading.Lock()
        self._load()

    # -- Public API ----------------------------------------------------------

    def create(
        self,
        name: str,
        content: str,
        description: str = "",
        model: Optional[str] = None,
        provider: Optional[str] = None,
        variables: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        created_by: str = "system",
        variants: Optional[dict[str, str]] = None,
    ) -> PromptTemplate:
        """Create a new prompt template.

        Variables are auto-detected from ``{{var}}`` patterns in *content*
        unless explicitly provided.

        Args:
            name: Human-readable template name.
            content: Template body with ``{{variable}}`` placeholders.
            description: Optional description.
            model: Recommended model for this template.
            provider: Recommended provider.
            variables: Explicit variable list; auto-detected if ``None``.
            tags: Classification tags.
            created_by: Creator identifier.
            variants: Optional A/B testing variants mapping name to content.

        Returns:
            The newly created ``PromptTemplate``.
        """
        now = time.time()
        template_id = str(uuid.uuid4())

        detected_vars = variables if variables is not None else _extract_variables(content)

        template = PromptTemplate(
            template_id=template_id,
            name=name,
            description=description,
            content=content,
            model=model,
            provider=provider,
            variables=detected_vars,
            tags=tags or [],
            version=1,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            variants=variants or {},
        )

        initial_version = PromptVersion(
            version=1,
            content=content,
            created_at=now,
            created_by=created_by,
            change_note="Initial version",
        )

        with self._lock:
            self._templates[template_id] = template
            self._versions[template_id] = [initial_version]
            self._save()

        return template

    def update(
        self,
        template_id: str,
        content: str,
        change_note: str = "",
        created_by: str = "system",
    ) -> PromptTemplate:
        """Update template content, creating a new version.

        Args:
            template_id: The template to update.
            content: New template body.
            change_note: Human-readable description of the change.
            created_by: Who made this change.

        Returns:
            The updated ``PromptTemplate``.

        Raises:
            KeyError: If *template_id* does not exist.
        """
        with self._lock:
            if template_id not in self._templates:
                raise KeyError(f"Template not found: {template_id}")

            template = self._templates[template_id]
            now = time.time()
            new_version = template.version + 1

            version_entry = PromptVersion(
                version=new_version,
                content=content,
                created_at=now,
                created_by=created_by,
                change_note=change_note,
            )

            # Update template in place
            template.content = content
            template.version = new_version
            template.updated_at = now
            template.variables = _extract_variables(content)

            self._versions.setdefault(template_id, []).append(version_entry)
            self._save()

        return template

    def render(
        self,
        template_id: str,
        variables: dict,
        variant: Optional[str] = None,
    ) -> str:
        """Render a template by substituting ``{{variable}}`` placeholders.

        Args:
            template_id: Template to render.
            variables: Mapping of variable name to value.
            variant: Optional variant name; uses main content if ``None``.

        Returns:
            Rendered string.

        Raises:
            KeyError: If *template_id* does not exist.
            ValueError: If a required variable is missing or variant is unknown.
        """
        with self._lock:
            if template_id not in self._templates:
                raise KeyError(f"Template not found: {template_id}")
            template = self._templates[template_id]

        # Determine which content to use
        if variant is not None:
            if variant not in template.variants:
                raise ValueError(
                    f"Unknown variant '{variant}'. Available: {list(template.variants.keys())}"
                )
            content = template.variants[variant]
        elif template.active_variant and template.active_variant in template.variants:
            content = template.variants[template.active_variant]
        else:
            content = template.content

        # Check for missing variables
        required = _extract_variables(content)
        missing = [v for v in required if v not in variables]
        if missing:
            raise ValueError(f"Missing required variables: {missing}")

        # Perform substitution
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            return str(variables.get(var_name, match.group(0)))

        return _VAR_PATTERN.sub(_replace, content)

    def get(self, template_id: str) -> Optional[PromptTemplate]:
        """Get a template by ID, or ``None`` if not found."""
        with self._lock:
            return self._templates.get(template_id)

    def list_all(self, tags: Optional[list[str]] = None) -> list[PromptTemplate]:
        """List all templates, optionally filtered by tags.

        Args:
            tags: If provided, only templates containing *all* of these tags
                  are returned.

        Returns:
            List of matching templates sorted by updated_at descending.
        """
        with self._lock:
            templates = list(self._templates.values())

        if tags:
            tag_set = set(tags)
            templates = [t for t in templates if tag_set.issubset(set(t.tags))]

        templates.sort(key=lambda t: t.updated_at, reverse=True)
        return templates

    def get_versions(self, template_id: str) -> list[PromptVersion]:
        """Get the full version history for a template.

        Args:
            template_id: Template to look up.

        Returns:
            List of versions sorted by version number ascending.

        Raises:
            KeyError: If *template_id* does not exist.
        """
        with self._lock:
            if template_id not in self._templates:
                raise KeyError(f"Template not found: {template_id}")
            versions = list(self._versions.get(template_id, []))

        versions.sort(key=lambda v: v.version)
        return versions

    def delete(self, template_id: str) -> bool:
        """Delete a template and its version history.

        Returns:
            ``True`` if the template was deleted, ``False`` if not found.
        """
        with self._lock:
            if template_id not in self._templates:
                return False
            del self._templates[template_id]
            self._versions.pop(template_id, None)
            self._save()
        return True

    # -- Persistence ---------------------------------------------------------

    def _save(self) -> None:
        """Flush current state to disk. Caller must hold ``_lock``."""
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "templates": {
                tid: t.model_dump() for tid, t in self._templates.items()
            },
            "versions": {
                tid: [v.model_dump() for v in vs]
                for tid, vs in self._versions.items()
            },
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
            for tid, tdata in raw.get("templates", {}).items():
                self._templates[tid] = PromptTemplate(**tdata)
            for tid, vlist in raw.get("versions", {}).items():
                self._versions[tid] = [PromptVersion(**v) for v in vlist]
        except (json.JSONDecodeError, Exception) as exc:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.warning("prompt_store_load_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

prompt_store = PromptStore()
