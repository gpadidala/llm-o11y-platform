"""Guardrails pipeline -- runs safety checks on LLM input and output.

Provides a configurable pipeline of guardrails that can:
  - Detect and redact PII (email, phone, SSN, credit cards, etc.)
  - Check content safety using keyword/pattern matching
  - Enforce topic restrictions
  - Validate output against JSON schemas or regex patterns
  - Block content matching custom regex patterns
"""

import json
import re
import time
from enum import Enum
from typing import Optional

import structlog
from pydantic import BaseModel

from src.guardrails.pii import PIIDetector, pii_detector
import src.otel.setup as otel_setup

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class GuardrailAction(str, Enum):
    """Action to take when a guardrail triggers."""

    ALLOW = "allow"
    BLOCK = "block"
    REDACT = "redact"
    WARN = "warn"


class GuardrailResult(BaseModel):
    """Result of a single guardrail check."""

    passed: bool
    action: GuardrailAction
    rule_name: str
    details: str = ""
    modified_content: Optional[str] = None  # For redact action
    latency_ms: float = 0.0


class GuardrailConfig(BaseModel):
    """Configuration for which guardrails are active and their parameters."""

    enable_pii_detection: bool = False
    enable_content_safety: bool = False
    enable_topic_restriction: bool = False
    blocked_topics: list[str] = []
    enable_output_validation: bool = False
    output_json_schema: Optional[dict] = None
    output_regex: Optional[str] = None
    max_output_tokens: Optional[int] = None
    custom_regex_blocks: list[str] = []  # Regex patterns to block


# ---------------------------------------------------------------------------
# Content safety keyword lists
# ---------------------------------------------------------------------------

# These are intentionally broad categories for demonstration. In production,
# a more sophisticated classifier or external API would be used.
_HARMFUL_PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:how\s+to\s+(?:make|build|create)\s+(?:a\s+)?(?:bomb|explosive|weapon))\b",
     "Potential weapons/explosives instruction request"),
    (r"\b(?:how\s+to\s+(?:hack|break\s+into|exploit)\s+(?:a\s+)?(?:system|server|network|account))\b",
     "Potential hacking instruction request"),
    (r"\b(?:how\s+to\s+(?:steal|forge|counterfeit))\b",
     "Potential illegal activity instruction request"),
    (r"\b(?:suicide\s+(?:method|instruction|how\s+to))\b",
     "Self-harm content detected"),
    (r"\b(?:child\s+(?:abuse|exploitation|pornography))\b",
     "CSAM-related content detected"),
]


# ---------------------------------------------------------------------------
# Guardrails Engine
# ---------------------------------------------------------------------------


class GuardrailsEngine:
    """Pipeline of input/output guardrails.

    The engine runs a configurable set of checks on both inbound messages
    (before they reach the LLM) and outbound content (before it reaches
    the user). Each check produces a ``GuardrailResult`` indicating whether
    the content passed, and what action to take if it didn't.
    """

    def __init__(self):
        self._default_config = GuardrailConfig()
        self._pii_detector = pii_detector

    @property
    def config(self) -> GuardrailConfig:
        """Return the current default configuration."""
        return self._default_config

    @config.setter
    def config(self, new_config: GuardrailConfig) -> None:
        """Update the default configuration."""
        self._default_config = new_config

    # -- Input checks --------------------------------------------------------

    async def check_input(
        self, messages: list[dict], config: Optional[GuardrailConfig] = None
    ) -> list[GuardrailResult]:
        """Run all enabled input guardrails on a list of chat messages.

        Args:
            messages: List of message dicts with ``role`` and ``content`` keys.
            config: Optional override config; uses default if ``None``.

        Returns:
            List of ``GuardrailResult`` for each check that was run.
        """
        cfg = config or self._default_config
        results: list[GuardrailResult] = []

        # Concatenate all message content for scanning
        combined = " ".join(msg.get("content", "") for msg in messages)

        if cfg.enable_pii_detection:
            pii_results = self.detect_pii(combined)
            results.extend(pii_results)
            self._emit_guardrail_metrics(pii_results)

        if cfg.enable_content_safety:
            safety_result = self.check_content_safety(combined)
            results.append(safety_result)
            self._emit_guardrail_metrics([safety_result])

        if cfg.enable_topic_restriction and cfg.blocked_topics:
            topic_result = self.check_topic_restriction(combined, cfg.blocked_topics)
            results.append(topic_result)
            self._emit_guardrail_metrics([topic_result])

        if cfg.custom_regex_blocks:
            for pattern in cfg.custom_regex_blocks:
                result = self._check_custom_regex(combined, pattern)
                results.append(result)
                self._emit_guardrail_metrics([result])

        return results

    # -- Output checks -------------------------------------------------------

    async def check_output(
        self, content: str, config: Optional[GuardrailConfig] = None
    ) -> list[GuardrailResult]:
        """Run all enabled output guardrails on LLM response content.

        Args:
            content: The LLM-generated output text.
            config: Optional override config; uses default if ``None``.

        Returns:
            List of ``GuardrailResult`` for each check that was run.
        """
        cfg = config or self._default_config
        results: list[GuardrailResult] = []

        if cfg.enable_pii_detection:
            pii_results = self.detect_pii(content)
            results.extend(pii_results)
            self._emit_guardrail_metrics(pii_results)

        if cfg.enable_content_safety:
            safety_result = self.check_content_safety(content)
            results.append(safety_result)
            self._emit_guardrail_metrics([safety_result])

        if cfg.enable_output_validation:
            if cfg.output_json_schema:
                json_result = self.validate_json_output(content, cfg.output_json_schema)
                results.append(json_result)
                self._emit_guardrail_metrics([json_result])
            if cfg.output_regex:
                regex_result = self.validate_regex_output(content, cfg.output_regex)
                results.append(regex_result)
                self._emit_guardrail_metrics([regex_result])

        if cfg.max_output_tokens is not None:
            length_result = self._check_output_length(content, cfg.max_output_tokens)
            results.append(length_result)
            self._emit_guardrail_metrics([length_result])

        if cfg.custom_regex_blocks:
            for pattern in cfg.custom_regex_blocks:
                result = self._check_custom_regex(content, pattern)
                results.append(result)
                self._emit_guardrail_metrics([result])

        return results

    # -- PII -----------------------------------------------------------------

    def detect_pii(self, text: str) -> list[GuardrailResult]:
        """Detect PII patterns in text.

        Returns one ``GuardrailResult`` per detected PII type. If no PII
        is found, returns a single passing result.

        Args:
            text: Input text to scan.

        Returns:
            List of guardrail results for each PII detection.
        """
        start = time.perf_counter()
        detection = self._pii_detector.scan(text)
        elapsed_ms = (time.perf_counter() - start) * 1000

        if not detection.matches:
            return [GuardrailResult(
                passed=True,
                action=GuardrailAction.ALLOW,
                rule_name="pii_detection",
                details="No PII detected",
                latency_ms=round(elapsed_ms, 2),
            )]

        results: list[GuardrailResult] = []
        for match in detection.matches:
            results.append(GuardrailResult(
                passed=False,
                action=GuardrailAction.WARN,
                rule_name=f"pii_detection_{match.pii_type}",
                details=(
                    f"Detected {match.pii_type} at position {match.start}-{match.end} "
                    f"(confidence: {match.confidence:.0%})"
                ),
                modified_content=detection.redacted_text,
                latency_ms=round(elapsed_ms, 2),
            ))

        return results

    def redact_pii(self, text: str) -> str:
        """Replace all detected PII with [REDACTED] tokens.

        Args:
            text: Input text to redact.

        Returns:
            Text with PII replaced by redaction labels.
        """
        return self._pii_detector.redact(text)

    # -- Content safety ------------------------------------------------------

    def check_content_safety(self, text: str) -> GuardrailResult:
        """Check text for harmful content using keyword/pattern matching.

        Args:
            text: Input text to check.

        Returns:
            A single ``GuardrailResult`` indicating safety status.
        """
        start = time.perf_counter()
        text_lower = text.lower()

        for pattern, description in _HARMFUL_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.warning(
                    "content_safety_violation",
                    description=description,
                )
                return GuardrailResult(
                    passed=False,
                    action=GuardrailAction.BLOCK,
                    rule_name="content_safety",
                    details=description,
                    latency_ms=round(elapsed_ms, 2),
                )

        elapsed_ms = (time.perf_counter() - start) * 1000
        return GuardrailResult(
            passed=True,
            action=GuardrailAction.ALLOW,
            rule_name="content_safety",
            details="Content passed safety check",
            latency_ms=round(elapsed_ms, 2),
        )

    # -- Topic restriction ---------------------------------------------------

    def check_topic_restriction(
        self, text: str, blocked_topics: list[str]
    ) -> GuardrailResult:
        """Check if text contains restricted topics.

        Uses case-insensitive substring matching against the blocked topics
        list. Matches on word boundaries when the topic is a single word.

        Args:
            text: Input text to check.
            blocked_topics: List of topic strings to block.

        Returns:
            A ``GuardrailResult`` indicating whether any topic was found.
        """
        start = time.perf_counter()
        text_lower = text.lower()
        triggered_topics: list[str] = []

        for topic in blocked_topics:
            topic_lower = topic.lower().strip()
            if not topic_lower:
                continue
            # Use word boundary matching for single-word topics
            if " " not in topic_lower:
                if re.search(rf"\b{re.escape(topic_lower)}\b", text_lower):
                    triggered_topics.append(topic)
            else:
                if topic_lower in text_lower:
                    triggered_topics.append(topic)

        elapsed_ms = (time.perf_counter() - start) * 1000

        if triggered_topics:
            return GuardrailResult(
                passed=False,
                action=GuardrailAction.BLOCK,
                rule_name="topic_restriction",
                details=f"Blocked topics detected: {', '.join(triggered_topics)}",
                latency_ms=round(elapsed_ms, 2),
            )

        return GuardrailResult(
            passed=True,
            action=GuardrailAction.ALLOW,
            rule_name="topic_restriction",
            details="No blocked topics found",
            latency_ms=round(elapsed_ms, 2),
        )

    # -- Output validation ---------------------------------------------------

    def validate_json_output(self, content: str, schema: dict) -> GuardrailResult:
        """Validate that output content is valid JSON matching a schema.

        Performs two checks:
        1. Is the content valid JSON?
        2. Does it contain the required keys from the schema?

        This uses a lightweight schema check (required keys + types) rather
        than full JSON Schema validation to avoid external dependencies.

        Args:
            content: LLM output to validate.
            schema: A dict describing expected structure. Supports
                    ``required`` (list of key names) and ``properties``
                    (dict of key -> {``type``: ...}).

        Returns:
            A ``GuardrailResult`` indicating validation status.
        """
        start = time.perf_counter()

        # Step 1: Parse JSON
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return GuardrailResult(
                passed=False,
                action=GuardrailAction.BLOCK,
                rule_name="json_output_validation",
                details=f"Invalid JSON: {exc}",
                latency_ms=round(elapsed_ms, 2),
            )

        # Step 2: Check required keys
        required_keys = schema.get("required", [])
        if required_keys and isinstance(parsed, dict):
            missing = [k for k in required_keys if k not in parsed]
            if missing:
                elapsed_ms = (time.perf_counter() - start) * 1000
                return GuardrailResult(
                    passed=False,
                    action=GuardrailAction.BLOCK,
                    rule_name="json_output_validation",
                    details=f"Missing required keys: {missing}",
                    latency_ms=round(elapsed_ms, 2),
                )

        # Step 3: Check property types
        properties = schema.get("properties", {})
        if properties and isinstance(parsed, dict):
            type_errors: list[str] = []
            _type_map = {
                "string": str, "number": (int, float), "integer": int,
                "boolean": bool, "array": list, "object": dict,
            }
            for key, prop_schema in properties.items():
                if key in parsed:
                    expected_type = prop_schema.get("type")
                    if expected_type and expected_type in _type_map:
                        if not isinstance(parsed[key], _type_map[expected_type]):
                            type_errors.append(
                                f"Key '{key}' expected {expected_type}, "
                                f"got {type(parsed[key]).__name__}"
                            )
            if type_errors:
                elapsed_ms = (time.perf_counter() - start) * 1000
                return GuardrailResult(
                    passed=False,
                    action=GuardrailAction.WARN,
                    rule_name="json_output_validation",
                    details=f"Type mismatches: {'; '.join(type_errors)}",
                    latency_ms=round(elapsed_ms, 2),
                )

        elapsed_ms = (time.perf_counter() - start) * 1000
        return GuardrailResult(
            passed=True,
            action=GuardrailAction.ALLOW,
            rule_name="json_output_validation",
            details="Output matches JSON schema",
            latency_ms=round(elapsed_ms, 2),
        )

    def validate_regex_output(self, content: str, pattern: str) -> GuardrailResult:
        """Validate that output content matches a regex pattern.

        Args:
            content: LLM output to validate.
            pattern: Regex pattern that the output must match (search, not fullmatch).

        Returns:
            A ``GuardrailResult`` indicating validation status.
        """
        start = time.perf_counter()

        try:
            if re.search(pattern, content, re.DOTALL):
                elapsed_ms = (time.perf_counter() - start) * 1000
                return GuardrailResult(
                    passed=True,
                    action=GuardrailAction.ALLOW,
                    rule_name="regex_output_validation",
                    details=f"Output matches pattern: {pattern}",
                    latency_ms=round(elapsed_ms, 2),
                )
            else:
                elapsed_ms = (time.perf_counter() - start) * 1000
                return GuardrailResult(
                    passed=False,
                    action=GuardrailAction.WARN,
                    rule_name="regex_output_validation",
                    details=f"Output does not match expected pattern: {pattern}",
                    latency_ms=round(elapsed_ms, 2),
                )
        except re.error as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return GuardrailResult(
                passed=False,
                action=GuardrailAction.WARN,
                rule_name="regex_output_validation",
                details=f"Invalid regex pattern: {exc}",
                latency_ms=round(elapsed_ms, 2),
            )

    # -- Metric emission ------------------------------------------------------

    @staticmethod
    def _emit_guardrail_metrics(results: list[GuardrailResult]) -> None:
        """Emit OTel metrics for a batch of guardrail results."""
        for result in results:
            # Every check gets a guardrail.checks counter tick
            if otel_setup.guardrail_checks is not None:
                otel_setup.guardrail_checks.add(
                    1, {"rule_name": result.rule_name, "action": result.action.value}
                )

            # Violations (anything that is not ALLOW)
            if result.action != GuardrailAction.ALLOW:
                if otel_setup.guardrail_violations is not None:
                    otel_setup.guardrail_violations.add(
                        1,
                        {
                            "rule_name": result.rule_name,
                            "action": result.action.value,
                            "violation_type": result.rule_name,
                        },
                    )

            # PII-specific metric: emit per PII type detected
            if result.rule_name.startswith("pii_detection_") and not result.passed:
                pii_type = result.rule_name.replace("pii_detection_", "")
                if otel_setup.guardrail_pii_detected is not None:
                    otel_setup.guardrail_pii_detected.add(1, {"pii_type": pii_type})

    # -- Private helpers -----------------------------------------------------

    def _check_output_length(self, content: str, max_tokens: int) -> GuardrailResult:
        """Check if output exceeds the maximum token count (estimated).

        Uses a simple word-count heuristic (~0.75 tokens per word) as a
        lightweight proxy for actual token counting.

        Args:
            content: LLM output text.
            max_tokens: Maximum allowed tokens.

        Returns:
            A ``GuardrailResult`` indicating length compliance.
        """
        # Rough token estimate: ~4 chars per token on average
        estimated_tokens = len(content) // 4

        if estimated_tokens > max_tokens:
            return GuardrailResult(
                passed=False,
                action=GuardrailAction.WARN,
                rule_name="output_length",
                details=(
                    f"Output exceeds max tokens: ~{estimated_tokens} estimated "
                    f"vs {max_tokens} allowed"
                ),
            )

        return GuardrailResult(
            passed=True,
            action=GuardrailAction.ALLOW,
            rule_name="output_length",
            details=f"Output within limits: ~{estimated_tokens} estimated tokens",
        )

    def _check_custom_regex(self, text: str, pattern: str) -> GuardrailResult:
        """Check if text matches a custom blocking regex pattern.

        Args:
            text: Text to check.
            pattern: Regex pattern that should NOT appear in the text.

        Returns:
            A ``GuardrailResult`` -- fails if the pattern matches.
        """
        start = time.perf_counter()

        try:
            match = re.search(pattern, text, re.IGNORECASE)
            elapsed_ms = (time.perf_counter() - start) * 1000

            if match:
                return GuardrailResult(
                    passed=False,
                    action=GuardrailAction.BLOCK,
                    rule_name="custom_regex_block",
                    details=f"Blocked pattern matched: {pattern} (at position {match.start()})",
                    latency_ms=round(elapsed_ms, 2),
                )

            return GuardrailResult(
                passed=True,
                action=GuardrailAction.ALLOW,
                rule_name="custom_regex_block",
                details=f"Pattern not found: {pattern}",
                latency_ms=round(elapsed_ms, 2),
            )

        except re.error as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return GuardrailResult(
                passed=False,
                action=GuardrailAction.WARN,
                rule_name="custom_regex_block",
                details=f"Invalid regex pattern '{pattern}': {exc}",
                latency_ms=round(elapsed_ms, 2),
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

guardrails_engine = GuardrailsEngine()
