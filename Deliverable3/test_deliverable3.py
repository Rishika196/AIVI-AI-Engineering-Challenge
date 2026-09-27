"""Unit and integration test suite for Deliverable 3.

All tests validate local logic, prompt formatting, Pydantic schema validation,
HTTP 429 backoff retry loops, and timeout cascades WITHOUT requiring real Gemini API calls.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

import httpx

# Ensure workspace root is in sys.path
workspace_root = Path(__file__).resolve().parent.parent
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from google.genai import errors
from Deliverable2.schema import (
    CandidateEvaluation,
    RateLimitExhaustedError,
    TimeoutFallbackError,
    SchemaValidationError,
)
from Deliverable3.ai_resume_matcher import (
    AIResumeMatcher,
    MissingAPIKeyError,
    EmptyInputError,
    TransientServiceError,
)


class TestDeliverable3(unittest.TestCase):
    def setUp(self):
        self.dummy_api_key = "test_fake_api_key_12345"
        self.sample_jd = (
            "Senior Backend Engineer requiring 5+ years Python, FastAPI, and Kubernetes."
        )
        self.sample_resume = (
            "Senior Software Engineer with 6 years experience building FastAPI services on Kubernetes."
        )
        self.valid_response_json = (
            "{\n"
            '  "match_score": 90,\n'
            '  "top_strengths": [\n'
            '    "6 years of production FastAPI experience",\n'
            '    "Demonstrated production Kubernetes deployment"\n'
            "  ],\n"
            '  "missing_skills": [],\n'
            '  "summary": "Candidate exhibits exceptional alignment with senior backend requirements.\\nExtensive FastAPI and Kubernetes experience directly fulfills all core criteria."\n'
            "}"
        )

    # ------------------------------------------------------------------------
    # 1. API Key & Initialization Tests
    # ------------------------------------------------------------------------
    @patch.dict("os.environ", {}, clear=True)
    def test_missing_api_key_raises_error(self):
        """Ensures MissingAPIKeyError is raised when no API key is in env or args."""
        with self.assertRaises(MissingAPIKeyError):
            AIResumeMatcher(api_key=None)

    def test_explicit_api_key_initialization(self):
        """Ensures matcher initializes cleanly when explicit key is provided."""
        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        self.assertIsNotNone(matcher.client)
        self.assertEqual(matcher.primary_model, "gemini-3.8-flash")

    # ------------------------------------------------------------------------
    # 2. Input Validation Tests
    # ------------------------------------------------------------------------
    def test_empty_resume_raises_error(self):
        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        with self.assertRaises(EmptyInputError) as ctx:
            matcher.evaluate(job_description=self.sample_jd, resume="")
        self.assertIn("resume", str(ctx.exception).lower())

    def test_empty_job_description_raises_error(self):
        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        with self.assertRaises(EmptyInputError) as ctx:
            matcher.evaluate(job_description="   ", resume=self.sample_resume)
        self.assertIn("job description", str(ctx.exception).lower())

    # ------------------------------------------------------------------------
    # 3. Prompt Formatting with Deliverable 2 Delimiters
    # ------------------------------------------------------------------------
    def test_prompt_formatting_delimiters(self):
        formatted = AIResumeMatcher.format_prompt(
            job_description=self.sample_jd,
            resume=self.sample_resume
        )
        self.assertIn("### JOB DESCRIPTION ###", formatted)
        self.assertIn("### CANDIDATE RESUME ###", formatted)
        self.assertTrue(formatted.startswith("### JOB DESCRIPTION ###"))
        self.assertIn(self.sample_jd, formatted)
        self.assertIn(self.sample_resume, formatted)

    # ------------------------------------------------------------------------
    # 4. Successful Mock Evaluation
    # ------------------------------------------------------------------------
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_successful_evaluation(self, mock_client_cls):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = self.valid_response_json
        mock_client.models.generate_content.return_value = mock_response
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        evaluation = matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)

        self.assertIsInstance(evaluation, CandidateEvaluation)
        self.assertEqual(evaluation.match_score, 90)
        self.assertEqual(len(evaluation.top_strengths), 2)
        self.assertEqual(evaluation.missing_skills, [])
        self.assertEqual(len(evaluation.summary.split("\n")), 2)

    # ------------------------------------------------------------------------
    # 5. HTTP 429 Rate Limit Retry Loop
    # ------------------------------------------------------------------------
    @patch("time.sleep", return_value=None)
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_429_retry_success_after_two_attempts(self, mock_client_cls, mock_sleep):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = self.valid_response_json

        # Create mock 429 error
        error_429 = errors.APIError(code=429, response_json={"error": "Resource has been exhausted"})

        # Fail twice with 429, succeed on 3rd attempt
        mock_client.models.generate_content.side_effect = [
            error_429,
            error_429,
            mock_response
        ]
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        evaluation = matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)

        self.assertEqual(evaluation.match_score, 90)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_429_retry_exhaustion_raises_error(self, mock_client_cls, mock_sleep):
        mock_client = MagicMock()
        error_429 = errors.APIError(code=429, response_json={"error": "Resource has been exhausted"})

        # Always raise 429
        mock_client.models.generate_content.side_effect = error_429
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        with self.assertRaises(RateLimitExhaustedError):
            matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)

        self.assertEqual(mock_sleep.call_count, 3)

    @patch("time.sleep", return_value=None)
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_503_retry_success_after_two_attempts(self, mock_client_cls, mock_sleep):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = self.valid_response_json

        error_503 = errors.APIError(code=503, response_json={"error": "Model is experiencing high demand"})

        mock_client.models.generate_content.side_effect = [
            error_503,
            error_503,
            mock_response
        ]
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        evaluation = matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)

        self.assertEqual(evaluation.match_score, 90)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_503_exhaustion_on_primary_triggers_fallback_model(self, mock_client_cls, mock_sleep):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = self.valid_response_json

        error_503 = errors.APIError(code=503, response_json={"error": "Model is experiencing high demand"})

        def side_effect_fn(model, contents, config):
            if model == "gemini-3.8-flash":  # Primary fails with 503
                raise error_503
            elif model == "gemini-3.7-flash":  # Fallback succeeds
                return mock_response
            raise ValueError(f"Unexpected model: {model}")

        mock_client.models.generate_content.side_effect = side_effect_fn
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(
            api_key=self.dummy_api_key,
            primary_model="gemini-3.8-flash",
            fallback_model="gemini-3.7-flash"
        )
        evaluation = matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)

        self.assertEqual(evaluation.match_score, 90)
        self.assertEqual(mock_sleep.call_count, 3)

    # ------------------------------------------------------------------------
    # 6. Timeout Fallback from PRIMARY_MODEL to FAST_FALLBACK_MODEL
    # ------------------------------------------------------------------------
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_timeout_fallback_to_secondary_model(self, mock_client_cls):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = self.valid_response_json

        def side_effect_fn(model, contents, config):
            if model == "gemini-2.5-flash":  # Primary
                raise httpx.ReadTimeout("Read timed out on primary model")
            elif model == "gemini-1.5-flash":  # Fallback
                return mock_response
            raise ValueError(f"Unexpected model: {model}")

        mock_client.models.generate_content.side_effect = side_effect_fn
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(
            api_key=self.dummy_api_key,
            primary_model="gemini-2.5-flash",
            fallback_model="gemini-1.5-flash"
        )
        evaluation = matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)
        self.assertEqual(evaluation.match_score, 90)
        self.assertEqual(mock_client.models.generate_content.call_count, 2)

    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_timeout_fallback_both_fail_raises_timeout_error(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = httpx.ConnectTimeout("Network deadline exceeded")
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        with self.assertRaises(TimeoutFallbackError):
            matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)

    # ------------------------------------------------------------------------
    # 7. Malformed Output & Schema Constraint Failures
    # ------------------------------------------------------------------------
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_malformed_json_raises_schema_error(self, mock_client_cls):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "This is not JSON at all."
        mock_client.models.generate_content.return_value = mock_response
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        with self.assertRaises(SchemaValidationError):
            matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)

    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_invalid_summary_lines_fails_schema_validation(self, mock_client_cls):
        mock_client = MagicMock()
        mock_response = MagicMock()
        # Invalid 3-line summary
        mock_response.text = (
            "{\n"
            '  "match_score": 75,\n'
            '  "top_strengths": ["Python"],\n'
            '  "missing_skills": [],\n'
            '  "summary": "Line 1\\nLine 2\\nLine 3"\n'
            "}"
        )
        mock_client.models.generate_content.return_value = mock_response
        mock_client_cls.return_value = mock_client

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        with self.assertRaises(SchemaValidationError) as ctx:
            matcher.evaluate(job_description=self.sample_jd, resume=self.sample_resume)
        self.assertIn("summary", str(ctx.exception).lower())

    # ------------------------------------------------------------------------
    # 8. Single File Input Evaluation
    # ------------------------------------------------------------------------
    @patch("Deliverable3.ai_resume_matcher.genai.Client")
    def test_evaluate_from_single_file(self, mock_client_cls):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = self.valid_response_json
        mock_client.models.generate_content.return_value = mock_response
        mock_client_cls.return_value = mock_client

        sample_file = Path(__file__).resolve().parent / "sample_input.txt"
        self.assertTrue(sample_file.exists())

        matcher = AIResumeMatcher(api_key=self.dummy_api_key)
        evaluation = matcher.evaluate_from_single_file(sample_file)
        self.assertEqual(evaluation.match_score, 90)


if __name__ == "__main__":
    unittest.main()
