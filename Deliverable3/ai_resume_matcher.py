"""Deliverable 3: AI Resume Matcher using Google Gemini API.

This module provides a standalone, production-ready implementation that compares
a candidate's raw resume against a target Job Description using Google's current
GenAI SDK (`google-genai`), enforcing strict structured outputs, schema validation,
HTTP 429 exponential backoff, and model-cascading timeout fallback.

All prompt architecture, Pydantic V2 schemas, and resilience utilities are reused
directly from Deliverable 2 without modification.
"""

from __future__ import annotations

import os
import sys
import time
import logging
import argparse
from pathlib import Path
from typing import Optional, Tuple, Union

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types, errors

# Ensure workspace root is in sys.path to import Deliverable 2
workspace_root = Path(__file__).resolve().parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from Deliverable2.schema import (
    CandidateEvaluation,
    parse_candidate_evaluation,
    get_system_prompt,
    calculate_backoff_delay,
    execute_with_timeout_fallback,
    PRIMARY_MODEL as D2_PRIMARY_MODEL,
    FAST_FALLBACK_MODEL as D2_FAST_FALLBACK_MODEL,
    MAX_RETRIES,
    DEFAULT_PRIMARY_TIMEOUT_SECONDS,
    DEFAULT_FALLBACK_TIMEOUT_SECONDS,
    EvaluationError,
    RateLimitExhaustedError,
    TimeoutFallbackError,
    SchemaValidationError,
)

# Load local .env if available
load_dotenv()

# Setup module logger
logger = logging.getLogger("ai_resume_matcher")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


# ============================================================================
# 1. Custom Domain Exceptions
# ============================================================================
class MissingAPIKeyError(EvaluationError):
    """Raised when the GEMINI_API_KEY environment variable is not provided."""
    pass


class EmptyInputError(EvaluationError):
    """Raised when either the resume or job description text is empty."""
    pass


class TransientServiceError(EvaluationError):
    """Raised when transient service errors (e.g. HTTP 503) exhaust retries."""
    pass


# ============================================================================
# 2. AI Resume Matcher Class
# ============================================================================
class AIResumeMatcher:
    """Production ATS evaluator integrating the Google Gemini API with Deliverable 2 architecture.
    
    Attributes:
        primary_model: Primary model for high-reasoning evaluation.
        fallback_model: Fast low-latency model for latency timeout cascades.
        primary_timeout: Request timeout in seconds for primary model.
        fallback_timeout: Request timeout in seconds for fast fallback model.
        system_prompt: Operational system instruction loaded from Deliverable 2.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        primary_model: Optional[str] = None,
        fallback_model: Optional[str] = None,
        primary_timeout: float = DEFAULT_PRIMARY_TIMEOUT_SECONDS,
        fallback_timeout: float = DEFAULT_FALLBACK_TIMEOUT_SECONDS,
        system_prompt: Optional[str] = None,
    ):
        """Initializes the AIResumeMatcher client.
        
        Args:
            api_key: Optional Gemini API key. Defaults to GEMINI_API_KEY env var.
            primary_model: Optional primary model name.
            fallback_model: Optional fallback model name.
            primary_timeout: Timeout in seconds for primary model calls.
            fallback_timeout: Timeout in seconds for fallback model calls.
            system_prompt: Optional custom system prompt string. Defaults to Deliverable 2 prompt.
            
        Raises:
            MissingAPIKeyError: If no API key is provided and GEMINI_API_KEY is unset.
        """
        resolved_key = api_key or os.getenv("GEMINI_API_KEY")
        if not resolved_key or not resolved_key.strip():
            raise MissingAPIKeyError(
                "GEMINI_API_KEY environment variable is missing or empty. "
                "Please configure GEMINI_API_KEY before running evaluations."
            )

        # Initialize the modern google-genai Client (never log or expose resolved_key)
        self.client = genai.Client(api_key=resolved_key.strip())

        # Resolve model names: preference to args -> env vars -> Deliverable 2 defaults -> modern defaults
        self.primary_model = (
            primary_model
            or os.getenv("GEMINI_PRIMARY_MODEL")
            or (D2_PRIMARY_MODEL if D2_PRIMARY_MODEL != "PRIMARY_MODEL" else "gemini-3.8-flash")
        )
        self.fallback_model = (
            fallback_model
            or os.getenv("GEMINI_FALLBACK_MODEL")
            or (D2_FAST_FALLBACK_MODEL if D2_FAST_FALLBACK_MODEL != "FAST_FALLBACK_MODEL" else "gemini-3.7-flash")
        )

        self.primary_timeout = float(primary_timeout)
        self.fallback_timeout = float(fallback_timeout)

        # Load production prompt architecture from Deliverable 2
        self.system_prompt = system_prompt or get_system_prompt()

    @staticmethod
    def format_prompt(job_description: str, resume: str) -> str:
        """Formats the input prompt using Deliverable 2 boundary delimiters.
        
        Args:
            job_description: Raw job description text.
            resume: Raw candidate resume text.
            
        Returns:
            Properly delimited prompt string for the LLM.
        """
        return (
            f"### JOB DESCRIPTION ###\n"
            f"{job_description.strip()}\n\n"
            f"### CANDIDATE RESUME ###\n"
            f"{resume.strip()}"
        )

    def _call_gemini_single_attempt(
        self,
        model_name: str,
        prompt: str,
        timeout: float
    ) -> str:
        """Executes a single generate_content API call with strict structured JSON config.
        
        Args:
            model_name: The Gemini model identifier.
            prompt: The formatted prompt containing delimiters.
            timeout: Maximum timeout in seconds for this attempt.
            
        Returns:
            Raw response text from Gemini.
            
        Raises:
            SchemaValidationError: If response is empty or blocked.
            httpx.TimeoutException / TimeoutError / errors.APIError: On network/API errors.
        """
        timeout_ms = int(timeout * 1000)
        config = types.GenerateContentConfig(
            system_instruction=self.system_prompt,
            response_mime_type="application/json",
            response_json_schema=CandidateEvaluation.model_json_schema(),
            http_options=types.HttpOptions(timeout=timeout_ms)
        )

        response = self.client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config
        )

        if not response.text:
            raise SchemaValidationError(
                f"Gemini model '{model_name}' returned an empty or filtered response."
            )

        return response.text

    def _call_with_retry_transient(
        self,
        model_name: str,
        prompt: str,
        timeout: float
    ) -> str:
        """Invokes Gemini with exponential backoff and additive jitter for transient errors (HTTP 429 and HTTP 503).
        
        Args:
            model_name: The Gemini model identifier.
            prompt: The formatted prompt.
            timeout: Timeout in seconds for each call.
            
        Returns:
            Raw response text from Gemini.
            
        Raises:
            RateLimitExhaustedError: If 429 retries exceed MAX_RETRIES.
            TransientServiceError: If 503 service capacity retries exceed MAX_RETRIES.
            Exception: Any non-transient exception raised by the SDK.
        """
        attempt = 0
        while attempt <= MAX_RETRIES:
            try:
                return self._call_gemini_single_attempt(model_name, prompt, timeout)
            except errors.APIError as api_err:
                err_code = getattr(api_err, "code", None)
                err_str = str(api_err)
                is_429 = (err_code == 429) or ("429" in err_str)
                is_503 = (err_code == 503) or ("503" in err_str) or ("UNAVAILABLE" in err_str)

                if is_429:
                    if attempt >= MAX_RETRIES:
                        logger.error(
                            f"HTTP 429 rate limit retries exhausted ({MAX_RETRIES} attempts) for model '{model_name}'."
                        )
                        raise RateLimitExhaustedError(
                            f"HTTP 429 Rate limit retries exhausted ({MAX_RETRIES} attempts) on model '{model_name}'."
                        ) from api_err

                    # Inspect response headers for Retry-After if available
                    retry_after_val = None
                    resp_obj = getattr(api_err, "response", None)
                    if resp_obj and hasattr(resp_obj, "headers"):
                        retry_after_val = resp_obj.headers.get("Retry-After") or resp_obj.headers.get("retry-after")

                    sleep_delay = calculate_backoff_delay(attempt=attempt, retry_after=retry_after_val)
                    logger.warning(
                        f"HTTP 429 on model '{model_name}' (attempt {attempt + 1}/{MAX_RETRIES}). "
                        f"Backing off for {sleep_delay:.2f}s..."
                    )
                    time.sleep(sleep_delay)
                    attempt += 1

                elif is_503:
                    if attempt >= MAX_RETRIES:
                        logger.error(
                            f"HTTP 503 service capacity retries exhausted ({MAX_RETRIES} attempts) for model '{model_name}'."
                        )
                        raise TransientServiceError(
                            f"HTTP 503 Service Unavailable retries exhausted ({MAX_RETRIES} attempts) on model '{model_name}'."
                        ) from api_err

                    sleep_delay = calculate_backoff_delay(attempt=attempt)
                    logger.warning(
                        f"HTTP 503 on model '{model_name}'; retrying after {sleep_delay:.2f} seconds..."
                    )
                    time.sleep(sleep_delay)
                    attempt += 1

                else:
                    # Non-transient API error (e.g. 400 Bad Request, 403 Forbidden, 404 Not Found)
                    raise

    def evaluate(self, job_description: str, resume: str) -> CandidateEvaluation:
        """Evaluates a candidate resume against a job description.
        
        Orchestration Pipeline:
        1. Validates non-empty inputs.
        2. Formats prompt with Deliverable 2 input delimiters.
        3. Invokes PRIMARY_MODEL.
        4. If PRIMARY_MODEL encounters a timeout/deadline or 503 exhaustion, falls back to FAST_FALLBACK_MODEL.
        5. Handles HTTP 429 and HTTP 503 transient errors via exponential backoff with additive jitter.
        6. Sanitizes raw output and strictly validates through Pydantic V2 schema.
        
        Args:
            job_description: Target job description text.
            resume: Candidate raw resume text.
            
        Returns:
            Validated CandidateEvaluation instance.
            
        Raises:
            EmptyInputError: If either input is empty or whitespace only.
            TimeoutFallbackError: If primary times out and fallback also fails.
            RateLimitExhaustedError: If HTTP 429 retries are exhausted.
            SchemaValidationError: If output fails Pydantic V2 schema constraints.
        """
        if not job_description or not job_description.strip():
            raise EmptyInputError("Job description text cannot be empty or whitespace only.")

        if not resume or not resume.strip():
            raise EmptyInputError("Candidate resume text cannot be empty or whitespace only.")

        prompt = self.format_prompt(job_description=job_description, resume=resume)

        # Define callables for primary and fallback models
        primary_caller = lambda: self._call_with_retry_transient(
            model_name=self.primary_model,
            prompt=prompt,
            timeout=self.primary_timeout
        )

        fallback_caller = lambda: self._call_with_retry_transient(
            model_name=self.fallback_model,
            prompt=prompt,
            timeout=self.fallback_timeout
        )

        def log_fallback_trigger(err: Exception) -> None:
            logger.warning(
                f"Primary model '{self.primary_model}' encountered {type(err).__name__} ({err}). "
                f"Falling back immediately to fast model '{self.fallback_model}'..."
            )

        # Reusable timeout and transient error fallback utility from Deliverable 2
        raw_output = execute_with_timeout_fallback(
            primary_caller=primary_caller,
            fallback_caller=fallback_caller,
            timeout_exceptions=(TimeoutError, httpx.TimeoutException, TransientServiceError),
            on_fallback=log_fallback_trigger
        )

        # Sanitize and strictly parse through Deliverable 2 CandidateEvaluation schema
        return parse_candidate_evaluation(raw_output)

    def evaluate_from_files(
        self,
        job_description_path: Union[str, Path],
        resume_path: Union[str, Path]
    ) -> CandidateEvaluation:
        """Reads inputs from separate files and executes evaluation."""
        jd_file = Path(job_description_path)
        res_file = Path(resume_path)

        if not jd_file.exists():
            raise FileNotFoundError(f"Job description file not found: {jd_file}")
        if not res_file.exists():
            raise FileNotFoundError(f"Resume file not found: {res_file}")

        jd_text = jd_file.read_text(encoding="utf-8")
        resume_text = res_file.read_text(encoding="utf-8")

        return self.evaluate(job_description=jd_text, resume=resume_text)

    def evaluate_from_single_file(
        self,
        input_file_path: Union[str, Path]
    ) -> CandidateEvaluation:
        """Parses a single input file containing delimited JD and resume sections.
        
        Expected file format:
            ### JOB DESCRIPTION ###
            ... text ...
            ### CANDIDATE RESUME ###
            ... text ...
        """
        target_path = Path(input_file_path)
        if not target_path.exists():
            raise FileNotFoundError(f"Input file not found: {target_path}")

        content = target_path.read_text(encoding="utf-8")

        jd_delimiter = "### JOB DESCRIPTION ###"
        resume_delimiter = "### CANDIDATE RESUME ###"

        if jd_delimiter not in content or resume_delimiter not in content:
            raise EmptyInputError(
                f"Input file must contain both '{jd_delimiter}' and '{resume_delimiter}' delimiters."
            )

        parts = content.split(resume_delimiter, 1)
        jd_part = parts[0].replace(jd_delimiter, "").strip()
        resume_part = parts[1].strip()

        return self.evaluate(job_description=jd_part, resume=resume_part)


# ============================================================================
# 3. Command Line Interface (CLI)
# ============================================================================
def build_cli_parser() -> argparse.ArgumentParser:
    """Builds and returns the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="AIVI Deliverable 3: AI Resume Matcher using Google Gemini API."
    )
    input_group = parser.add_argument_group("Input Options")
    input_group.add_argument(
        "--input", "-i",
        type=str,
        help="Path to a single file containing delimited '### JOB DESCRIPTION ###' and '### CANDIDATE RESUME ###'."
    )
    input_group.add_argument(
        "--jd", "-j",
        type=str,
        help="Path to job description text file."
    )
    input_group.add_argument(
        "--resume", "-r",
        type=str,
        help="Path to candidate resume text file."
    )

    model_group = parser.add_argument_group("Model & Timeout Options")
    model_group.add_argument(
        "--primary-model",
        type=str,
        default=None,
        help="Primary model name (default: GEMINI_PRIMARY_MODEL env var or gemini-3.8-flash)."
    )
    model_group.add_argument(
        "--fallback-model",
        type=str,
        default=None,
        help="Fast fallback model name (default: GEMINI_FALLBACK_MODEL env var or gemini-3.7-flash)."
    )
    model_group.add_argument(
        "--primary-timeout",
        type=float,
        default=DEFAULT_PRIMARY_TIMEOUT_SECONDS,
        help=f"Timeout in seconds for primary model (default: {DEFAULT_PRIMARY_TIMEOUT_SECONDS}s)."
    )
    model_group.add_argument(
        "--fallback-timeout",
        type=float,
        default=DEFAULT_FALLBACK_TIMEOUT_SECONDS,
        help=f"Timeout in seconds for fallback model (default: {DEFAULT_FALLBACK_TIMEOUT_SECONDS}s)."
    )

    output_group = parser.add_argument_group("Output Options")
    output_group.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Optional path to write the output JSON."
    )
    return parser


def main() -> int:
    """CLI execution entry point."""
    parser = build_cli_parser()
    args = parser.parse_args()

    # Determine input mode
    if args.input:
        jd_path = None
        resume_path = None
        single_input_path = args.input
    elif args.jd and args.resume:
        jd_path = args.jd
        resume_path = args.resume
        single_input_path = None
    else:
        # Default fallback to sample_input.txt if present
        default_sample = Path(__file__).resolve().parent / "sample_input.txt"
        if default_sample.exists():
            logger.info(f"No inputs specified. Using default sample input: {default_sample}")
            single_input_path = str(default_sample)
            jd_path = None
            resume_path = None
        else:
            parser.error("Please provide either --input <file> OR both --jd <file> and --resume <file>.")
            return 1

    try:
        matcher = AIResumeMatcher(
            primary_model=args.primary_model,
            fallback_model=args.fallback_model,
            primary_timeout=args.primary_timeout,
            fallback_timeout=args.fallback_timeout
        )

        if single_input_path:
            evaluation = matcher.evaluate_from_single_file(single_input_path)
        else:
            evaluation = matcher.evaluate_from_files(jd_path, resume_path)

        # Output pure structured JSON
        json_output = evaluation.model_dump_json(indent=2)
        print(json_output)

        if args.output:
            out_file = Path(args.output)
            out_file.write_text(json_output, encoding="utf-8")
            logger.info(f"Evaluation JSON saved to: {out_file}")

        return 0

    except MissingAPIKeyError as key_err:
        logger.error(f"Configuration Error: {key_err}")
        return 2
    except EmptyInputError as input_err:
        logger.error(f"Input Error: {input_err}")
        return 3
    except TimeoutFallbackError as timeout_err:
        logger.error(f"Timeout Fallback Error: {timeout_err}")
        return 4
    except RateLimitExhaustedError as rate_err:
        logger.error(f"Rate Limit Error: {rate_err}")
        return 5
    except TransientServiceError as trans_err:
        logger.error(f"Service Capacity Error: {trans_err}")
        return 7
    except SchemaValidationError as schema_err:
        logger.error(f"Validation Error: {schema_err}")
        return 6
    except Exception as exc:
        logger.error(f"Execution Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
