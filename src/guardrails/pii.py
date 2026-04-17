"""Comprehensive PII detection with regex-based pattern matching.

Detects the following PII types:
  - Email addresses
  - Phone numbers (US, UK, international)
  - Social Security Numbers (SSN)
  - Credit card numbers (Visa, Mastercard, Amex, Discover)
  - IP addresses (IPv4, IPv6)
  - Dates of birth
  - Physical addresses (basic US street patterns)
  - API keys / tokens (common patterns: sk-, ghp_, xoxb-, etc.)

Each detector returns structured results with PII type, location,
confidence score, and a redacted version of the text.
"""

import re
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PIIMatch(BaseModel):
    """A single PII detection result."""

    pii_type: str
    value: str
    start: int
    end: int
    confidence: float  # 0.0 - 1.0
    redacted_value: str


class PIIDetectionResult(BaseModel):
    """Aggregated PII detection result for a text input."""

    original_text: str
    redacted_text: str
    matches: list[PIIMatch]
    total_found: int


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Each entry: (pii_type, compiled regex, confidence, redaction label)
_PII_PATTERNS: list[tuple[str, re.Pattern, float, str]] = []


def _register(pii_type: str, pattern: str, confidence: float, label: str, flags: int = 0) -> None:
    """Register a PII pattern for detection."""
    _PII_PATTERNS.append((pii_type, re.compile(pattern, flags), confidence, label))


# --- Email ---
_register(
    "email",
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    0.95,
    "[EMAIL_REDACTED]",
)

# --- US Phone numbers ---
# Matches: (123) 456-7890, 123-456-7890, 123.456.7890, +1 123 456 7890
_register(
    "phone_us",
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)",
    0.85,
    "[PHONE_REDACTED]",
)

# --- UK Phone numbers ---
# Matches: +44 7911 123456, 07911 123456, +44 20 7946 0958
_register(
    "phone_uk",
    r"(?<!\d)(?:\+44[\s.\-]?|0)(?:7\d{3}|\d{2,4})[\s.\-]?\d{3,4}[\s.\-]?\d{3,4}(?!\d)",
    0.80,
    "[PHONE_REDACTED]",
)

# --- International phone ---
# Matches: +<country_code> followed by 7-14 digits with optional separators
_register(
    "phone_international",
    r"(?<!\d)\+(?!1\b|44\b)\d{1,3}[\s.\-]?\d{2,4}[\s.\-]?\d{3,4}[\s.\-]?\d{3,4}(?!\d)",
    0.70,
    "[PHONE_REDACTED]",
)

# --- SSN ---
# Matches: 123-45-6789, 123 45 6789
_register(
    "ssn",
    r"(?<!\d)\d{3}[\s\-]\d{2}[\s\-]\d{4}(?!\d)",
    0.90,
    "[SSN_REDACTED]",
)

# --- Credit card: Visa ---
_register(
    "credit_card_visa",
    r"(?<!\d)4\d{3}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)",
    0.90,
    "[CREDIT_CARD_REDACTED]",
)

# --- Credit card: Mastercard ---
_register(
    "credit_card_mastercard",
    r"(?<!\d)5[1-5]\d{2}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)",
    0.90,
    "[CREDIT_CARD_REDACTED]",
)

# --- Credit card: Amex ---
_register(
    "credit_card_amex",
    r"(?<!\d)3[47]\d{2}[\s\-]?\d{6}[\s\-]?\d{5}(?!\d)",
    0.90,
    "[CREDIT_CARD_REDACTED]",
)

# --- Credit card: Discover ---
_register(
    "credit_card_discover",
    r"(?<!\d)6(?:011|5\d{2})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)",
    0.90,
    "[CREDIT_CARD_REDACTED]",
)

# --- IPv4 ---
_register(
    "ip_address_v4",
    r"(?<!\d)(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\."
    r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?!\d)",
    0.80,
    "[IP_REDACTED]",
)

# --- IPv6 (simplified — matches common full and abbreviated forms) ---
_register(
    "ip_address_v6",
    r"(?<![:\w])(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}(?![:\w])",
    0.75,
    "[IP_REDACTED]",
)

# --- IPv6 abbreviated (with ::) ---
_register(
    "ip_address_v6_abbrev",
    r"(?<![:\w])(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}(?![:\w])",
    0.70,
    "[IP_REDACTED]",
)

# --- Date of birth ---
# Matches: MM/DD/YYYY, MM-DD-YYYY, DD/MM/YYYY, YYYY-MM-DD
_register(
    "date_of_birth",
    r"(?<!\d)(?:\d{1,2}[/\-]\d{1,2}[/\-]\d{4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})(?!\d)",
    0.60,
    "[DOB_REDACTED]",
)

# --- Physical address (basic US) ---
# Matches patterns like: 123 Main St, 456 Oak Avenue, etc.
_register(
    "address_us",
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s?){1,3}"
    r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Road|Rd|Court|Ct"
    r"|Way|Place|Pl|Circle|Cir|Trail|Trl|Parkway|Pkwy)\b",
    0.65,
    "[ADDRESS_REDACTED]",
    re.IGNORECASE,
)

# --- API keys / tokens ---
# OpenAI secret key
_register(
    "api_key_openai",
    r"\bsk-[A-Za-z0-9]{20,}(?:\b|$)",
    0.95,
    "[API_KEY_REDACTED]",
)

# GitHub Personal Access Token
_register(
    "api_key_github",
    r"\bghp_[A-Za-z0-9]{36,}\b",
    0.95,
    "[API_KEY_REDACTED]",
)

# GitHub OAuth token
_register(
    "api_key_github_oauth",
    r"\bgho_[A-Za-z0-9]{36,}\b",
    0.95,
    "[API_KEY_REDACTED]",
)

# Slack token
_register(
    "api_key_slack",
    r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b",
    0.90,
    "[API_KEY_REDACTED]",
)

# AWS access key ID
_register(
    "api_key_aws",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    0.95,
    "[API_KEY_REDACTED]",
)

# Generic long hex/base64 token (40+ chars, heuristic)
_register(
    "api_key_generic",
    r"(?:api[_-]?key|token|secret|password|bearer)\s*[=:]\s*['\"]?([A-Za-z0-9+/=\-_]{40,})['\"]?",
    0.70,
    "[API_KEY_REDACTED]",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Detection engine
# ---------------------------------------------------------------------------


class PIIDetector:
    """Stateless PII detector using compiled regex patterns.

    Usage::

        detector = PIIDetector()
        result = detector.scan("Contact me at john@example.com")
        print(result.redacted_text)
        # => "Contact me at [EMAIL_REDACTED]"
    """

    def __init__(self, patterns: Optional[list[tuple[str, re.Pattern, float, str]]] = None):
        self._patterns = patterns if patterns is not None else _PII_PATTERNS

    def scan(self, text: str) -> PIIDetectionResult:
        """Scan text for all known PII patterns.

        Args:
            text: The input text to scan.

        Returns:
            A ``PIIDetectionResult`` with matches and a fully redacted version.
        """
        matches: list[PIIMatch] = []
        occupied: list[tuple[int, int]] = []  # Track matched ranges to avoid overlaps

        for pii_type, pattern, confidence, label in self._patterns:
            for m in pattern.finditer(text):
                start, end = m.start(), m.end()
                # Avoid overlapping matches
                if any(s <= start < e or s < end <= e for s, e in occupied):
                    continue
                value = m.group(0)
                matches.append(PIIMatch(
                    pii_type=pii_type,
                    value=value,
                    start=start,
                    end=end,
                    confidence=confidence,
                    redacted_value=label,
                ))
                occupied.append((start, end))

        # Sort matches by position for consistent redaction
        matches.sort(key=lambda m: m.start)

        # Build redacted text
        redacted = self._redact(text, matches)

        return PIIDetectionResult(
            original_text=text,
            redacted_text=redacted,
            matches=matches,
            total_found=len(matches),
        )

    def scan_messages(self, messages: list[dict]) -> list[PIIDetectionResult]:
        """Scan a list of chat messages for PII.

        Args:
            messages: List of dicts with at least a ``content`` key.

        Returns:
            One ``PIIDetectionResult`` per message.
        """
        return [self.scan(msg.get("content", "")) for msg in messages]

    def redact(self, text: str) -> str:
        """Convenience method: scan and return redacted text."""
        return self.scan(text).redacted_text

    def _redact(self, text: str, matches: list[PIIMatch]) -> str:
        """Replace matched PII with redaction labels.

        Processes matches in reverse order to preserve character positions.
        """
        if not matches:
            return text
        chars = list(text)
        for match in reversed(matches):
            chars[match.start:match.end] = list(match.redacted_value)
        return "".join(chars)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

pii_detector = PIIDetector()
