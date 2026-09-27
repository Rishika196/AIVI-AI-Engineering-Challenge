"""Deliverable 2: Pydantic Schema and Fallback Architecture.

This module provides:
1. Strict Pydantic V2 schema validation for candidate resume evaluations.
2. Strict summary validation (exactly two non-empty lines separated by one newline).
3. Sanitization and parsing helpers with extra-field prohibition.
4. HTTP 429 exponential backoff with additive jitter and Retry-After support.
5. Practical timeout fallback utility (PRIMARY_MODEL -> FAST_FALLBACK_MODEL -> TimeoutFallbackError).
6. System prompt loading utility for seamless reuse in Deliverable 3.
"""

from __future__ import annotations

import os
import re
import json
import random
from pathlib import Path
from typing import Callable, List, Optional, TypeVar, Union
from pydantic import BaseModel, Field, field_validator, ConfigDict, ValidationError

T = TypeVar("T")


# ============================================================================
# 1. Abstract Model Configuration & Fallback Constants
# ============================================================================
# Model names are decoupled from specific provider tags and configured via
# environment variables or Deliverable 3 runtime parameters.
PRIMARY_MODEL: str = os.getenv("GEMINI_PRIMARY_MODEL", "PRIMARY_MODEL")
FAST_FALLBACK_MODEL: str = os.getenv("GEMINI_FALLBACK_MODEL", "FAST_FALLBACK_MODEL")

# HTTP 429 Rate Limit Constants (Exponential Backoff with Additive Jitter)
MAX_RETRIES: int = 3
INITIAL_BACKOFF_SECONDS: float = 2.0
BACKOFF_FACTOR: float = 2.0
MAX_BACKOFF_SECONDS: float = 30.0
JITTER_MIN_SECONDS: float = 0.1
JITTER_MAX_SECONDS: float = 1.0

# Timeout & Latency Fallback Constants
DEFAULT_PRIMARY_TIMEOUT_SECONDS: float = 12.0
DEFAULT_FALLBACK_TIMEOUT_SECONDS: float = 8.0


# ============================================================================
# 2. Custom Domain Exceptions
# ============================================================================
class EvaluationError(Exception):
    """Base exception for candidate evaluation pipeline errors."""
    pass


class RateLimitExhaustedError(EvaluationError):
    """Raised when HTTP 429 rate limit retries are exhausted."""
    pass


class TimeoutFallbackError(EvaluationError):
    """Raised when primary model times out and fast fallback model also fails."""
    pass


class SchemaValidationError(EvaluationError):
    """Raised when model output fails strict Pydantic V2 schema validation."""
    pass


# ============================================================================
# 3. Pydantic V2 Candidate Evaluation Schema
# ============================================================================
class CandidateEvaluation(BaseModel):
    """Strict schema enforcing deterministic evaluation outputs.
    
    Fields:
        match_score: Integer from 0 to 100 representing alignment against JD.
        top_strengths: Up to 5 concrete, evidence-backed qualifications matching JD.
        missing_skills: JD requirements absent or lacking depth in resume.
        summary: Exactly two non-empty lines separated by '\\n'.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "match_score": 85,
                "top_strengths": [
                    "5+ years production experience with Python and FastAPI microservices",
                    "Demonstrated design of high-throughput distributed architectures",
                    "Extensive experience with PostgreSQL query optimization and indexing"
                ],
                "missing_skills": [
                    "Hands-on Kubernetes cluster administration in production"
                ],
                "summary": (
                    "The candidate presents a strong senior backend engineering background with proven distributed systems experience.\n"
                    "Their production Python and database tuning align closely with core requirements despite limited Kubernetes orchestration."
                )
            }
        }
    )

    match_score: int = Field(
        ...,
        ge=0,
        le=100,
        description=(
            "Integer score from 0 to 100 representing overall alignment based on the "
            "weighted rubric and mandatory ceiling rule."
        )
    )

    top_strengths: List[str] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "0 to 5 concrete, evidence-backed strengths directly matching JD requirements. "
            "Qualified candidates must have 3-5 items; weak candidates 1-2 items; completely "
            "unaligned or unparseable resumes must return an empty list []. Never invent placeholder strings."
        )
    )

    missing_skills: List[str] = Field(
        default_factory=list,
        description=(
            "List of explicit requirements, tools, frameworks, or competencies from the Job Description "
            "that are absent or insufficiently substantiated in the resume."
        )
    )

    summary: str = Field(
        ...,
        description=(
            "A concise summary of exactly 2 lines separated by a single newline character (\\n). "
            "Line 1 evaluates candidate profile alignment; Line 2 details the decisive justification or mandatory cap."
        )
    )

    @field_validator("summary")
    @classmethod
    def validate_exact_two_lines(cls, value: str) -> str:
        """Enforces that the summary contains exactly 2 non-empty lines separated by a single newline.
        
        Strict rules:
        - Must contain exactly one newline character ('\\n').
        - Splitting by '\\n' must yield exactly 2 items.
        - Neither line may be empty or contain only whitespace.
        - Does NOT silently remove or skip arbitrary blank lines.
        - Carriage returns ('\\r') are forbidden.
        """
        if not isinstance(value, str):
            raise ValueError("Summary must be a string.")

        if "\r" in value:
            raise ValueError("Summary must not contain carriage return ('\\r') characters; use standard '\\n'.")

        lines = value.split("\n")
        if len(lines) != 2:
            raise ValueError(
                f"Summary must contain exactly 2 non-empty lines separated by a single newline character ('\\n'). "
                f"Found {len(lines)} line(s)."
            )

        line1, line2 = lines[0].strip(), lines[1].strip()
        if not line1 or not line2:
            raise ValueError("Both lines in summary must be non-empty and contain meaningful text.")

        return f"{line1}\n{line2}"


# ============================================================================
# 4. JSON Sanitization & Parsing Helpers
# ============================================================================
def sanitize_and_extract_json(raw_text: str) -> str:
    """Strips markdown codeblocks, preambles, and extracts outermost valid JSON object.
    
    Args:
        raw_text: Raw string returned by the LLM.
        
    Returns:
        Clean JSON substring.
        
    Raises:
        SchemaValidationError: If no JSON object structure ({...}) can be located.
    """
    if not raw_text or not isinstance(raw_text, str):
        raise SchemaValidationError("Raw text is empty or invalid.")

    text = raw_text.strip()

    # Strip markdown code fences if present (```json ... ``` or ``` ... ```)
    markdown_pattern = r"^```(?:json)?\s*([\s\S]*?)\s*```$"
    match = re.search(markdown_pattern, text, re.IGNORECASE)
    if match:
        text = match.group(1).strip()

    # Extract outermost JSON object { ... }
    first_brace = text.find("{")
    last_brace = text.rfind("}")

    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return text[first_brace : last_brace + 1]

    raise SchemaValidationError(f"Could not locate a valid JSON object in response: {raw_text[:100]}...")


def parse_candidate_evaluation(raw_text: str) -> CandidateEvaluation:
    """Parses, sanitizes, and strictly validates raw LLM output into CandidateEvaluation.
    
    Args:
        raw_text: Raw LLM response string.
        
    Returns:
        Validated CandidateEvaluation instance.
        
    Raises:
        SchemaValidationError: If JSON is invalid or fails Pydantic schema validation.
    """
    clean_json = sanitize_and_extract_json(raw_text)

    try:
        return CandidateEvaluation.model_validate_json(clean_json)
    except (ValidationError, json.JSONDecodeError) as err:
        raise SchemaValidationError(f"CandidateEvaluation validation failed: {err}") from err


# ============================================================================
# 5. Standalone HTTP 429 Backoff & Timeout Fallback Utilities
# ============================================================================
def calculate_backoff_delay(
    attempt: int,
    retry_after: Optional[Union[float, int, str]] = None,
    initial_backoff: float = INITIAL_BACKOFF_SECONDS,
    backoff_factor: float = BACKOFF_FACTOR,
    max_backoff: float = MAX_BACKOFF_SECONDS,
    jitter_min: float = JITTER_MIN_SECONDS,
    jitter_max: float = JITTER_MAX_SECONDS
) -> float:
    """Calculates sleep delay for HTTP 429 using exponential backoff with additive jitter.
    
    Formula:
        If retry_after is provided and valid: float(retry_after) + uniform(jitter_min, jitter_max)
        Else: min(max_backoff, initial_backoff * (backoff_factor ** attempt)) + uniform(jitter_min, jitter_max)
        
    Args:
        attempt: Zero-indexed attempt number (0, 1, 2, ...).
        retry_after: Optional Retry-After value from HTTP response header.
        initial_backoff: Initial base backoff in seconds.
        backoff_factor: Exponential multiplier.
        max_backoff: Maximum backoff ceiling.
        jitter_min: Minimum additive jitter in seconds.
        jitter_max: Maximum additive jitter in seconds.
        
    Returns:
        Float number of seconds to sleep before retrying.
    """
    jitter = random.uniform(jitter_min, jitter_max)

    if retry_after is not None:
        try:
            parsed_retry = float(retry_after)
            if parsed_retry > 0:
                return parsed_retry + jitter
        except (ValueError, TypeError):
            pass

    exponential = initial_backoff * (backoff_factor ** attempt)
    capped_wait = min(max_backoff, exponential)
    return capped_wait + jitter


def execute_with_timeout_fallback(
    primary_caller: Callable[[], T],
    fallback_caller: Callable[[], T],
    timeout_exceptions: tuple[type[Exception], ...] = (TimeoutError,),
    on_fallback: Optional[Callable[[Exception], None]] = None,
) -> T:
    """Executes primary_caller and falls back to fallback_caller on timeout/deadline error.

    Architecture Flow:
        PRIMARY_MODEL (primary_caller)
            │
            │ timeout / deadline error (matches timeout_exceptions)
            ▼
        FAST_FALLBACK_MODEL (fallback_caller)
            │
            │ failure (any Exception)
            ▼
        TimeoutFallbackError

    Args:
        primary_caller: Zero-argument callable invoking the primary model.
        fallback_caller: Zero-argument callable invoking the fast fallback model.
        timeout_exceptions: Tuple of exception types indicating a timeout or deadline exceeded.
        on_fallback: Optional callback receiving the primary timeout exception for logging.

    Returns:
        The successful return value from primary_caller or fallback_caller.

    Raises:
        TimeoutFallbackError: If primary times out AND fallback also fails.
        Exception: If primary fails with an error other than a timeout (e.g. authentication).
    """
    try:
        return primary_caller()
    except timeout_exceptions as primary_err:
        if on_fallback:
            try:
                on_fallback(primary_err)
            except Exception:
                pass
        try:
            return fallback_caller()
        except Exception as fallback_err:
            raise TimeoutFallbackError(
                f"Primary model timed out with {type(primary_err).__name__}: {primary_err}. "
                f"Fast fallback model also failed with {type(fallback_err).__name__}: {fallback_err}."
            ) from fallback_err


def get_system_prompt(file_path: Optional[Union[str, Path]] = None) -> str:
    """Loads the production system prompt from Deliverable2/system_prompt.md.
    
    Args:
        file_path: Optional custom path to system_prompt.md. Defaults to the
                   sibling system_prompt.md file in the same directory.
                   
    Returns:
        UTF-8 string content of the system prompt.
    """
    if file_path is None:
        target_path = Path(__file__).resolve().parent / "system_prompt.md"
    else:
        target_path = Path(file_path)

    if not target_path.exists():
        raise FileNotFoundError(f"System prompt file not found at: {target_path}")

    return target_path.read_text(encoding="utf-8")
