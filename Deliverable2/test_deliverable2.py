"""Comprehensive validation suite for Deliverable 2.

Covers:
1. match_score > 100
2. match_score < 0
3. more than 5 top_strengths
4. missing required field
5. extra unexpected JSON field
6. malformed JSON
7. invalid summary formatting (1 line, 3 lines, blank lines, carriage returns)
8. valid summary formatting
9. HTTP 429 backoff behavior (exponential with additive jitter, Retry-After support)
10. timeout fallback behavior (primary success, failover to fallback, exhaustion to TimeoutFallbackError)
11. system prompt loading and integrity
"""

import sys
import unittest
from pathlib import Path
from pydantic import ValidationError

# Ensure workspace root is in sys.path
workspace_dir = Path(__file__).resolve().parent.parent
if str(workspace_dir) not in sys.path:
    sys.path.insert(0, str(workspace_dir))

from Deliverable2.schema import (
    CandidateEvaluation,
    parse_candidate_evaluation,
    calculate_backoff_delay,
    execute_with_timeout_fallback,
    get_system_prompt,
    SchemaValidationError,
    TimeoutFallbackError,
)


class TestDeliverable2(unittest.TestCase):
    def setUp(self):
        self.valid_payload = {
            "match_score": 85,
            "top_strengths": [
                "5+ years backend engineering in Python",
                "Proven microservices design",
                "Deep PostgreSQL optimization skills"
            ],
            "missing_skills": ["Production Kubernetes cluster management"],
            "summary": (
                "The candidate possesses strong backend development credentials with demonstrated microservices expertise.\n"
                "Their database performance optimization aligns closely with core job requirements."
            )
        }

    # ------------------------------------------------------------------------
    # 1. match_score > 100
    # ------------------------------------------------------------------------
    def test_match_score_greater_than_100(self):
        data = dict(self.valid_payload)
        data["match_score"] = 101
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("match_score", str(ctx.exception))

    # ------------------------------------------------------------------------
    # 2. match_score < 0
    # ------------------------------------------------------------------------
    def test_match_score_less_than_0(self):
        data = dict(self.valid_payload)
        data["match_score"] = -1
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("match_score", str(ctx.exception))

    # ------------------------------------------------------------------------
    # 3. more than 5 top_strengths
    # ------------------------------------------------------------------------
    def test_more_than_5_top_strengths(self):
        data = dict(self.valid_payload)
        data["top_strengths"] = [f"Strength {i}" for i in range(1, 7)]  # 6 items
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("top_strengths", str(ctx.exception))

    # ------------------------------------------------------------------------
    # 4. missing required field
    # ------------------------------------------------------------------------
    def test_missing_required_field_summary(self):
        data = dict(self.valid_payload)
        del data["summary"]
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("summary", str(ctx.exception))

    def test_missing_required_field_match_score(self):
        data = dict(self.valid_payload)
        del data["match_score"]
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("match_score", str(ctx.exception))

    # ------------------------------------------------------------------------
    # 5. extra unexpected JSON field
    # ------------------------------------------------------------------------
    def test_extra_unexpected_json_field(self):
        data = dict(self.valid_payload)
        data["unexpected_bonus_field"] = "hallucinated_data"
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("unexpected_bonus_field", str(ctx.exception))
        self.assertIn("extra_forbidden", str(ctx.exception))

    # ------------------------------------------------------------------------
    # 6. malformed JSON
    # ------------------------------------------------------------------------
    def test_malformed_json_unclosed_brace(self):
        malformed = '{"match_score": 50, "summary": "Unfinished JSON'
        with self.assertRaises(SchemaValidationError):
            parse_candidate_evaluation(malformed)

    def test_malformed_json_no_json_object(self):
        no_json = "I am an AI assistant and here is your analysis: perfectly qualified!"
        with self.assertRaises(SchemaValidationError):
            parse_candidate_evaluation(no_json)

    # ------------------------------------------------------------------------
    # 7. invalid summary formatting
    # ------------------------------------------------------------------------
    def test_invalid_summary_single_line(self):
        data = dict(self.valid_payload)
        data["summary"] = "Candidate is qualified but lacks some skills."
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("Summary must contain exactly 2 non-empty lines", str(ctx.exception))

    def test_invalid_summary_three_lines(self):
        data = dict(self.valid_payload)
        data["summary"] = "Line 1\nLine 2\nLine 3"
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("Summary must contain exactly 2 non-empty lines", str(ctx.exception))

    def test_invalid_summary_internal_blank_lines(self):
        data = dict(self.valid_payload)
        data["summary"] = "Line 1\n\nLine 2"
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("Summary must contain exactly 2 non-empty lines", str(ctx.exception))

    def test_invalid_summary_trailing_newline(self):
        data = dict(self.valid_payload)
        data["summary"] = "Line 1\nLine 2\n"
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("Summary must contain exactly 2 non-empty lines", str(ctx.exception))

    def test_invalid_summary_carriage_return(self):
        data = dict(self.valid_payload)
        data["summary"] = "Line 1\r\nLine 2"
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("carriage return", str(ctx.exception))

    def test_invalid_summary_empty_line(self):
        data = dict(self.valid_payload)
        data["summary"] = "Line 1\n   "
        with self.assertRaises(ValidationError) as ctx:
            CandidateEvaluation.model_validate(data)
        self.assertIn("Both lines in summary must be non-empty", str(ctx.exception))

    # ------------------------------------------------------------------------
    # 8. valid summary formatting
    # ------------------------------------------------------------------------
    def test_valid_summary_formatting(self):
        data = dict(self.valid_payload)
        data["summary"] = "Candidate profile matches senior requirements.\nStrong backend architecture experience justifies the score."
        model = CandidateEvaluation.model_validate(data)
        lines = model.summary.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(bool(lines[0].strip()))
        self.assertTrue(bool(lines[1].strip()))

    # ------------------------------------------------------------------------
    # 9. existing 429 backoff behavior (exponential with additive jitter)
    # ------------------------------------------------------------------------
    def test_429_exponential_backoff_delays(self):
        # Attempt 0: base 2.0 * (2^0) = 2.0 + jitter [0.1, 1.0] -> [2.1, 3.0]
        delay0 = calculate_backoff_delay(0)
        self.assertTrue(2.1 <= delay0 <= 3.0)

        # Attempt 1: base 2.0 * (2^1) = 4.0 + jitter [0.1, 1.0] -> [4.1, 5.0]
        delay1 = calculate_backoff_delay(1)
        self.assertTrue(4.1 <= delay1 <= 5.0)

        # Attempt 2: base 2.0 * (2^2) = 8.0 + jitter [0.1, 1.0] -> [8.1, 9.0]
        delay2 = calculate_backoff_delay(2)
        self.assertTrue(8.1 <= delay2 <= 9.0)

        # Max backoff ceiling cap: attempt 10 should be capped at 30.0 + jitter
        delay10 = calculate_backoff_delay(10)
        self.assertTrue(30.1 <= delay10 <= 31.0)

    def test_429_retry_after_header_support(self):
        # Retry-After header takes precedence over formula
        delay_header = calculate_backoff_delay(0, retry_after=15.0)
        self.assertTrue(15.1 <= delay_header <= 16.0)

    # ------------------------------------------------------------------------
    # 10. timeout fallback behavior
    # ------------------------------------------------------------------------
    def test_timeout_fallback_primary_success(self):
        calls = []

        def primary():
            calls.append("primary")
            return "primary_result"

        def fallback():
            calls.append("fallback")
            return "fallback_result"

        result = execute_with_timeout_fallback(primary, fallback)
        self.assertEqual(result, "primary_result")
        self.assertEqual(calls, ["primary"])

    def test_timeout_fallback_on_primary_timeout(self):
        calls = []

        def primary():
            calls.append("primary")
            raise TimeoutError("Connection to PRIMARY_MODEL timed out")

        def fallback():
            calls.append("fallback")
            return "fallback_success"

        result = execute_with_timeout_fallback(primary, fallback)
        self.assertEqual(result, "fallback_success")
        self.assertEqual(calls, ["primary", "fallback"])

    def test_timeout_fallback_both_fail_raises_custom_error(self):
        calls = []

        def primary():
            calls.append("primary")
            raise TimeoutError("PRIMARY_MODEL read timeout after 12.0s")

        def fallback():
            calls.append("fallback")
            raise RuntimeError("FAST_FALLBACK_MODEL 503 Service Unavailable")

        with self.assertRaises(TimeoutFallbackError) as ctx:
            execute_with_timeout_fallback(primary, fallback)

        self.assertIn("PRIMARY_MODEL read timeout", str(ctx.exception))
        self.assertIn("FAST_FALLBACK_MODEL 503", str(ctx.exception))
        self.assertEqual(calls, ["primary", "fallback"])

    def test_timeout_fallback_primary_non_timeout_not_masked(self):
        calls = []

        def primary():
            calls.append("primary")
            raise ValueError("Invalid API Key / Unauthorized")

        def fallback():
            calls.append("fallback")
            return "fallback_success"

        # Non-timeout error in primary should not be masked as a timeout fallback error
        with self.assertRaises(ValueError):
            execute_with_timeout_fallback(primary, fallback)

        self.assertEqual(calls, ["primary"])

    # ------------------------------------------------------------------------
    # 11. System prompt loading & content integrity
    # ------------------------------------------------------------------------
    def test_system_prompt_loading_and_delimiters(self):
        prompt = get_system_prompt()
        self.assertIn("### JOB DESCRIPTION ###", prompt)
        self.assertIn("### CANDIDATE RESUME ###", prompt)
        self.assertIn("Unified Mandatory Ceiling Rule", prompt)
        self.assertIn("The 40-Point Cap", prompt)
        self.assertIn("additive jitter", prompt)
        self.assertGreater(len(prompt), 1000)


if __name__ == "__main__":
    unittest.main()
