# Deliverable 3: AI Resume Matcher (Gemini API Integration)

**Project:** AIVI Intelligence AI Engineering Challenge  
**Module:** Deliverable 3  
**SDK:** Google GenAI Python SDK (`google-genai`)

---

## Overview

Deliverable 3 implements a standalone, production-ready AI resume evaluation script. It ingests a candidate's raw resume text alongside a target Job Description (JD) and evaluates alignment against the deterministic rubric established in Deliverable 2.

All prompt architectures, schemas, and resilience strategies from **Deliverable 2** are reused directly without modification.

---

## Deliverable 3 File Structure

```
Deliverable3/
├── ai_resume_matcher.py    # Main evaluation engine and CLI interface
├── requirements.txt        # Production dependencies (google-genai, pydantic, etc.)
├── sample_input.txt        # Production-grade sample Job Description and Resume
├── test_deliverable3.py    # Unit & integration test suite (mocked Gemini API)
└── README.md               # Complete setup, architecture, and execution guide
```

---

## What Each File Does

| File | Purpose |
| :--- | :--- |
| **`ai_resume_matcher.py`** | The core evaluation engine. Encapsulates the `AIResumeMatcher` class and CLI entrypoint. Connects to the Gemini API using `google-genai`, enforces structured outputs with `CandidateEvaluation`, orchestrates transient error retries (HTTP 429 and HTTP 503), and executes model failover on latency spikes/timeouts. |
| **`requirements.txt`** | Pins runtime dependencies: `google-genai>=2.25.0`, `pydantic>=2.12.5`, `python-dotenv>=1.0.0`, and `httpx>=0.28.1`. |
| **`sample_input.txt`** | Provides realistic sample inputs partitioned by `### JOB DESCRIPTION ###` and `### CANDIDATE RESUME ###` for quick demonstration and integration testing. |
| **`test_deliverable3.py`** | A 15-test automated test suite verifying all local logic, rate limit and service capacity retry loops, timeout cascades, and schema validation without making live network calls to Google's API. |
| **`README.md`** | Complete user and reviewer documentation. |

---

## How Deliverable 3 Reuses Deliverable 2

Deliverable 3 imports directly from `Deliverable2.schema` without modifying any file in `Deliverable2/`:

```python
from Deliverable2.schema import (
    CandidateEvaluation,             # Strict Pydantic V2 schema
    parse_candidate_evaluation,       # Sanitizer & validator
    get_system_prompt,               # Loader for Deliverable2/system_prompt.md
    calculate_backoff_delay,         # 429 exponential backoff with additive jitter
    execute_with_timeout_fallback,   # Primary -> fallback model timeout cascade
    PRIMARY_MODEL, FAST_FALLBACK_MODEL,
    MAX_RETRIES,
    DEFAULT_PRIMARY_TIMEOUT_SECONDS,
    DEFAULT_FALLBACK_TIMEOUT_SECONDS,
    EvaluationError, RateLimitExhaustedError,
    TimeoutFallbackError, SchemaValidationError
)
```

1. **Prompt Architecture:** Loaded dynamically via `get_system_prompt()`, ensuring prompt updates in `Deliverable2/system_prompt.md` immediately reflect in Deliverable 3.
2. **Schema Integrity:** The same Pydantic V2 `CandidateEvaluation` model enforces constraints across both deliverables.
3. **Resilience Utilities:** Reuses the standalone `calculate_backoff_delay` and `execute_with_timeout_fallback` functions directly.

---

## Structured Output & Gemini API Enforcement

Structured JSON output is guaranteed through a **two-layer enforcement model**:

### 1. Gemini API Server-Side Schema Enforcement
Using the modern `google-genai` SDK:
```python
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
```
- `response_mime_type="application/json"` guarantees that the Gemini engine outputs raw JSON without conversational preambles.
- `response_json_schema=CandidateEvaluation.model_json_schema()` forces token generation to conform to the JSON Schema generated from `CandidateEvaluation`.

### 2. Client-Side Sanitization & Pydantic V2 Validation
The raw response text is processed by `parse_candidate_evaluation(raw_text)`:
- Strips any accidental markdown wrappers (```` ```json ... ``` ````).
- Extracts outermost `{...}` braces via brace matching.
- Strictly validates against `CandidateEvaluation.model_validate_json(clean_json)`:
  - Verifies `match_score` is an integer between `0` and `100`.
  - Verifies `top_strengths` contains at most 5 items.
  - Verifies `summary` contains **exactly two non-empty lines** separated by `\n` without arbitrary blank lines.
  - Rejects extra unexpected fields (`model_config = ConfigDict(extra="forbid")`).

---

## Error & Fault Handling Strategy

| Scenario | Handled By | Behavior |
| :--- | :--- | :--- |
| **Missing API Key** | `MissingAPIKeyError` | Checks `GEMINI_API_KEY` before initializing `genai.Client`. Never logs or prints the key. Exits with code `2`. |
| **Empty Resume / Empty JD** | `EmptyInputError` | Pre-flight validation checks both inputs are non-empty and non-whitespace before API calls. Exits with code `3`. |
| **HTTP 429 Rate Limit** | `_call_with_retry_transient` | Catches `errors.APIError` (code 429). Inspects `Retry-After` header. If absent, applies exponential backoff with additive jitter: $\min(30.0, 2.0 \times 2^{\text{attempt}}) + \text{uniform}(0.1, 1.0)$. Retries up to 3 times before raising `RateLimitExhaustedError` (code `5`). |
| **HTTP 503 Capacity Spikes** | `_call_with_retry_transient` | Catches `errors.APIError` (code 503 / `UNAVAILABLE`). Retries up to 3 times using bounded exponential backoff with additive jitter. If retries are exhausted on `PRIMARY_MODEL`, raises `TransientServiceError` to seamlessly trigger failover to `FAST_FALLBACK_MODEL`. |
| **Timeout / Latency Spike** | `execute_with_timeout_fallback` | Executes call against `PRIMARY_MODEL` (default: `gemini-3.8-flash`). On `httpx.TimeoutException`, `TimeoutError`, or `TransientServiceError` (503 exhaustion), automatically routes to `FAST_FALLBACK_MODEL` (default: `gemini-3.7-flash`). If both fail, raises `TimeoutFallbackError` (code `4`). |
| **Malformed Output** | `SchemaValidationError` | Pre-parsing catches missing braces or non-JSON strings. Exits with code `6`. |
| **Constraint Violation** | `SchemaValidationError` | Rejects summary with $\neq 2$ lines, negative scores, $>5$ strengths, or extra fields. Exits with code `6`. |

---

## Setup & Installation

### 1. Install Dependencies

Ensure Python 3.10+ is installed, then run:

```bash
pip install -r Deliverable3/requirements.txt
```

### 2. Configure GEMINI_API_KEY on Windows

#### In PowerShell:
```powershell
$env:GEMINI_API_KEY="your_api_key_here"
```

#### In Windows Command Prompt (`cmd.exe`):
```cmd
set GEMINI_API_KEY=your_api_key_here
```

#### Using a `.env` file (Optional):
Create a `.env` file in the project root:
```env
GEMINI_API_KEY=your_api_key_here
GEMINI_PRIMARY_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODEL=gemini-3.7-flash
```

---

## Running the Evaluator

### Mode 1: Quick Run with Default Sample Input
If no arguments are provided, the script automatically uses `Deliverable3/sample_input.txt`:

```bash
python Deliverable3/ai_resume_matcher.py
```

### Mode 2: Run with a Single Delimited File
```bash
python Deliverable3/ai_resume_matcher.py --input Deliverable3/sample_input.txt
```

### Mode 3: Run with Separate Job Description and Resume Files
```bash
python Deliverable3/ai_resume_matcher.py --jd path/to/jd.txt --resume path/to/resume.txt
```

### Mode 4: Save Output to a JSON File
```bash
python Deliverable3/ai_resume_matcher.py --input Deliverable3/sample_input.txt --output evaluation_result.json
```

### Mode 5: Custom Models & Timeouts
```bash
python Deliverable3/ai_resume_matcher.py \
  --input Deliverable3/sample_input.txt \
  --primary-model gemini-3.8-flash \
  --fallback-model gemini-3.7-flash \
  --primary-timeout 15.0 \
  --fallback-timeout 8.0
```

---

## Expected Output Format

The script outputs strictly formatted JSON conforming to `CandidateEvaluation`:

```json
{
  "match_score": 88,
  "top_strengths": [
    "6+ years architecting Python FastAPI microservices on AWS EKS serving 15k RPS",
    "Hands-on experience deploying LLM evaluation pipelines with structured Pydantic outputs",
    "Demonstrated database query optimization reducing PostgreSQL CPU utilization by 40%"
  ],
  "missing_skills": [
    "Production experience with vector databases (Pinecone, Qdrant)"
  ],
  "summary": "The candidate presents exceptional technical alignment with senior backend and AI infrastructure requirements.\nTheir production Kubernetes, FastAPI, and database optimization experience directly satisfies all mandatory criteria."
}
```

---

## Running the Test Suites

### Run Deliverable 3 Test Suite (15 Tests):
Tests all local logic, rate limit loops, service capacity retries, timeout cascades, and schema constraints using mocks (no real API key required):

```bash
python -m unittest Deliverable3/test_deliverable3.py -v
```

### Run Deliverable 2 Test Suite (22 Tests):
Verifies the underlying prompt, scoring rules, and schema validators:

```bash
python -m unittest Deliverable2/test_deliverable2.py -v
```

### Run All Workspace Tests (37 Tests Combined):
```bash
python -m unittest Deliverable2/test_deliverable2.py Deliverable3/test_deliverable3.py -v
```

**Combined Test Results:**
```text
Ran 37 tests in 0.303s

OK
```
* **Deliverable 2 Tests:** 22 passed, 0 failed.
* **Deliverable 3 Tests:** 15 passed, 0 failed.
* **Combined Workspace Tests:** 37 passed, 0 failed.
