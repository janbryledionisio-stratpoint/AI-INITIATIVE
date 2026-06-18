# AI Initiative — TC Analyzer

Automated test case quality analyzer. Fetches test cases from TestRail, applies a general standards pre-filter, then uses an LLM to evaluate the quality of preconditions, test steps, and expected results against documented standards.

---

## How it works

```
data/<input>.csv
    ↓ generate_endpoints.py   — extract TestRail IDs → build API endpoint list
    ↓ fetch_cases.py          — fetch each case from TestRail API
    ↓ general_standards_filter.py  — apply §2 metadata criteria (Automatability, Testing Type, etc.)
    → output/testcases_with_standards_<timestamp>.csv

output/testcases_with_standards_<timestamp>.csv
    ↓ fetch_passed_cases.py   — filter rows where General Standards == "Passed"
    ↓ validate_cases.py       — LLM evaluates §3 quality standards per case
    → output/llm_validation_<timestamp>.csv
```

---

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Copy `.env.example` to `.env` and fill in credentials:

```
TESTRAIL_USERNAME=your@email.com
TESTRAIL_PASSWORD=your_testrail_api_key

# At least one LLM provider key
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GOOGLE_API_KEY=...
# Ollama and Groq require no key
```

---

## Usage

### Fetch test cases from TestRail

```bash
uv run python main.py fetch --input-csv data/GRCX-web.csv
```

Reads `ID` and `Testrail Link` columns from the input CSV, fetches full case details from the TestRail API, applies the general standards filter, and saves results to `output/testcases_with_standards_<timestamp>.csv`.

### Validate passed cases with an LLM

```bash
uv run python main.py validate --provider openai
```

Reads the latest `testcases_with_standards_*.csv` from `output/`, sends each passed case to the LLM for quality evaluation, and saves results to `output/llm_validation_<timestamp>.csv`.

### Run both steps end-to-end

```bash
uv run python main.py run-all --input-csv data/GRCX-web.csv --provider anthropic
```

### Provider options

| Provider | Flag | Default model | Notes |
|---|---|---|---|
| Anthropic | `--provider anthropic` | `claude-opus-4-8` | Requires `ANTHROPIC_API_KEY` |
| OpenAI | `--provider openai` | `gpt-4o` | Requires `OPENAI_API_KEY` |
| Google | `--provider google` | `gemini-2.0-flash` | Requires `GOOGLE_API_KEY`; use `--delay 4` on free tier |
| Ollama | `--provider ollama` | `llama3.3` | No key needed; requires local Ollama install |

Override the model with `--model`, e.g. `--model gpt-4o-mini`.

Use `--delay <seconds>` to throttle requests for rate-limited free-tier keys:

```bash
uv run python main.py validate --provider google --delay 4
```

---

## Output files

**`output/testcases_with_standards_<timestamp>.csv`**

| Column | Description |
|---|---|
| General Standards | `Passed` or `Failed` |
| Remarks | Unmet §2 criteria (blank if passed) |
| Test Case ID | e.g. `C1097771` |
| Title, Testing Type, Testing Phase, Status, etc. | Metadata from TestRail |
| Pre Conditions, Test Steps, Expected Result | Case content |

**`output/llm_validation_<timestamp>.csv`**

| Column | Description |
|---|---|
| Test Case ID | e.g. `C1097771` |
| Title | Case title |
| Preconditions Passed | `True` / `False` |
| Preconditions Remarks | Specific issues found |
| Test Steps Passed | `True` / `False` |
| Test Steps Remarks | Specific issues found |
| Expected Results Passed | `True` / `False` |
| Expected Results Remarks | Specific issues found |
| Overall Passed | `True` if all three criteria pass |
| Summary | One-sentence quality assessment |

---

## Quality standards evaluated by LLM

See [requirements.md](requirements.md) §3 for the full criteria. In brief:

- **§3.2 Preconditions (Web)** — must include URL, credentials, and environment
- **§3.3 Test Steps** — atomic (one action per step), imperative wording, exact data specified for every input action, each step has a corresponding expected result
- **§3.4 Expected Results** — explicitly defined, objective, measurable, binary pass/fail condition

General standards (§2 — Automatability, Testing Type, Automation Status, Status, Priority, Testing Phase) are enforced by the pre-filter before LLM validation runs.

---

## Project structure

```
main.py                     — CLI entry point (fetch / validate / run-all)
generate_endpoints.py       — builds TestRail endpoint list from input CSV
fetch_cases.py              — fetches test case data from TestRail API
general_standards_filter.py — applies §2 metadata criteria filter
fetch_passed_cases.py       — loads latest output CSV, returns passed cases
validate_cases.py           — LLM validation logic (multi-provider)
requirements.md             — quality standards reference (fed to LLM)
data/                       — input CSVs (TestRail exports)
endpoints/                  — generated endpoint CSVs (intermediate)
output/                     — output CSVs (standards filter + LLM validation)
```
