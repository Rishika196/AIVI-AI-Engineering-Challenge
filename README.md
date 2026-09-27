# AIVI AI Engineer Challenge

This repository contains the implementations for the AIVI AI Engineer Challenge.

## Deliverable 2 – System Prompt Architecture

Contains:
- Production ATS evaluator system prompt with deterministic scoring rubric and few-shot edge cases
- Strict Pydantic V2 validation schema (`CandidateEvaluation`)
- Unit and integration tests (22 tests)
- Technical architecture documentation

## Deliverable 3 – Working Python AI Script

Contains:
- Production Gemini-powered resume-JD matching engine (`AIResumeMatcher`)
- Structured JSON output enforcement via `google-genai` SDK
- HTTP 429 and HTTP 503 transient error handling with exponential backoff and jitter
- Latency timeout failover cascading from primary to fast fallback model
- Sample resume and job description input
- Unit and integration tests with mocked API responses (15 tests)
- Execution instructions

---

## Setup & API Key Configuration

To run the application, provide your Google Gemini API key via the `GEMINI_API_KEY` environment variable. 

> **Security Note:** Never hard-code API keys or credentials in source code. Do not commit `.env` files containing real secrets (the project `.gitignore` automatically excludes `.env` files).

### Setting `GEMINI_API_KEY` Locally

#### Windows (PowerShell):
```powershell
$env:GEMINI_API_KEY="your_api_key_here"
```

#### Windows (Command Prompt):
```cmd
set GEMINI_API_KEY=your_api_key_here
```

#### Linux / macOS (Bash / Zsh):
```bash
export GEMINI_API_KEY="your_api_key_here"
```

#### Using a Local `.env` File:
You may create a local `.env` file in the project root (ignored by Git):
```env
GEMINI_API_KEY=your_api_key_here
GEMINI_PRIMARY_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODEL=gemini-3.7-flash
```

---

## Running the Application & Tests

### Install Dependencies:
```bash
pip install -r Deliverable3/requirements.txt
```

### Run Resume Matcher:
```bash
python Deliverable3/ai_resume_matcher.py
```

### Run Test Suites:
```bash
# Run Deliverable 2 tests (22 tests)
python -m unittest Deliverable2/test_deliverable2.py -v

# Run Deliverable 3 tests (15 tests)
python -m unittest Deliverable3/test_deliverable3.py -v

# Run all combined workspace tests (37 tests)
python -m unittest Deliverable2/test_deliverable2.py Deliverable3/test_deliverable3.py -v
```