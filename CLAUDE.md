# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment & Commands

This project uses **`uv`** for dependency and environment management (`pyproject.toml` + `uv.lock`, Python >= 3.13). Do **not** invoke bare `python` — the system interpreter lacks the dependencies and will raise `ModuleNotFoundError: No module named 'pandas'`. Always go through the project venv:

```bash
uv sync                          # install/refresh deps into .venv
uv run python main.py run-all    # fetch + filter, then LLM-validate (CLI)
uv run python main.py fetch      # fetch from TestRail + general standards filter
uv run python main.py validate   # LLM-validate previously-passed cases
uv run streamlit run app.py      # launch the interactive UI (http://localhost:8501)
uv run python <file>.py          # run any script
```

`main.py` is an argparse CLI with three subcommands (`fetch`, `validate`,
`run-all`); see `uv run python main.py <cmd> --help` for flags (`--input-csv`,
`--output-dir`, `--provider`, `--model`, `--delay`). There are no tests, linter, or
build step configured.

### Running the UI
A [Streamlit](https://streamlit.io) front-end ([app.py](app.py)) drives the same
pipeline interactively. Launch it (do **not** use bare `streamlit`):

```bash
uv run streamlit run app.py   # serves on http://localhost:8501
```

From the sidebar you pick a TestRail export (upload a raw CSV or choose an existing
file in `data/`), supply credentials (defaults to `.env`; never displays the API key —
a manual override is injected into the environment for the run), and hit **Run
analysis**. Stage 1 fetches from TestRail (concurrent) and applies the General
Standards filter, then the page shows summary metrics, a filterable/searchable results
table with CSV download, a per-case detail drill-down, and a failure-by-criterion
breakdown. The **🧪 LLM Validation** tab is the optional Stage 2: it runs the *passed*
cases through the LLM quality evaluator (pick a provider/model), then shows pass/fail/
error metrics and a downloadable report.

The UI calls the same functions as the CLI: `generate_endpoint_file` /
`fetch_test_cases` from `testrail/`, `save_all_cases` from `pipeline/`, and
`validate_cases` from `validation/`. It writes the same timestamped outputs under
`output/`, and on first load (no run yet) falls back to the latest report via
`pipeline.loader.get_latest_output_file`.

### Credentials
The fetch stage requires a `.env` file (gitignored, not committed) with TestRail credentials:

```
TESTRAIL_USERNAME=<testrail-login-email>
TESTRAIL_PASSWORD=<testrail-api-key>   # the API KEY, not the account password
```

TestRail has password auth disabled, so `TESTRAIL_PASSWORD` must hold an **API key** (My Settings -> API Keys in TestRail). Treat these as use-but-never-display: load via `dotenv` and pass to `requests`; never `cat`/`echo`/print the values into output.

## Requirements

The full initiative requirements live in @docs/requirements.md — consult it when
changing the General Standards criteria or the filtering logic. Keep that file and
`evaluate_case()` in [general_standards_filter.py](general_standards_filter.py) in sync.

## Architecture

A 3-stage ETL pipeline that pulls test cases from TestRail's API and flags which ones meet the automation "General Standards". There are two entry points over the same stages: `main.py` (CLI) and `app.py` (Streamlit UI). `main.py` orchestrates the stages in order:

1. **Generate endpoints** — [generate_endpoints.py](generate_endpoints.py): reads a raw TestRail UI export (e.g. `data/GRCX-web.csv`), extracts the numeric case ID from each `Testrail Link` via regex `/view/(\d+)`, builds the API v2 URL (`.../api/v2/get_case/<id>`), and writes `endpoints/<name>-endpoint.csv`.

2. **Fetch** — [fetch_cases.py](fetch_cases.py): reads the endpoints CSV and does a `GET` per row with HTTP Basic Auth. Returns `[{ID, status_code, response(JSON)}]`. Errors are caught per-row so one failure doesn't halt the run. This is ~300 sequential calls and takes a few minutes; there is no throttling/backoff.

3. **Evaluate & save** — [general_standards_filter.py](general_standards_filter.py): the core logic. Skips non-200 responses, transforms each TestRail JSON case into a flat row, evaluates it against the General Standards, sorts Passed-before-Failed, and writes `testcases_with_standards.csv` (UTF-8-BOM for Excel).

The UI ([app.py](app.py)) calls the same package functions as the CLI — it does
**not** keep its own copy of the pipeline logic. It runs `save_all_cases()` to write
the report, then reads the timestamped CSV back into a DataFrame for display; the
optional LLM stage runs `validate_cases()` on the passed subset. Since
`fetch_test_cases()` reads credentials from the environment, the UI injects a manual
credential override into `os.environ` before the run.

### Data flow
`data/<name>.csv` (raw export) -> `endpoints/<name>-endpoint.csv` (intermediate) -> `testcases_with_standards.csv` (final Pass/Fail report). The UI reads the same final CSV on first load.

### The General Standards rule
`evaluate_case()` is the source of truth (the parallel `is_valid_case()` is **dead code** — same logic, not called anywhere). A case **Passes** only if ALL hold:

| Field (TestRail custom field) | Allowed IDs | Meaning |
|---|---|---|
| `custom_automatability` | 2, 4 | Automatable, To be Determined |
| `custom_testing_type` | 17 | web |
| `custom_automation_status` | 1, 9 | Not Started, Reviewed = Passed DOR |
| `case_status_id` | 2, 3, 6 | New, Reviewed, Active |
| `priority_id` | not null | — |
| `custom_testing_phase` | any of 1, 2, 12 | BE Regression, FE Regression, Regression |

Failures produce a human-readable `Remarks` string listing each unmet criterion.

### Numeric ID <-> label maps
TestRail returns custom fields as integer IDs. The maps at the top of [general_standards_filter.py](general_standards_filter.py) (`AUTOMATABILITY_MAP`, `AUTOMATION_STATUS_MAP`, `TESTING_TYPE_MAP`, `STATUS_MAP`, `TESTING_PHASE_MAP`) translate IDs to the labels written to the output CSV. When auditing the output CSV you match against these **label strings** (e.g. `"web"`, `"Not Started"`), not the raw IDs. Note `Testing Phase` is a comma-joined multi-value string — split on `, ` and match exact items (`"Regression"` is a substring of `"BE Regression"`).

### Gotchas
- The input `GRCX-web.csv` is already web-only, so in practice every case satisfies Testing Type, Automatability, Status, and Testing Phase — **Automation Status is currently the only discriminating field** (blank vs. "Not Started"). Keep this in mind when a filtering change appears to have no effect.
- HTML fields (preconds, steps, expected results) are cleaned via BeautifulSoup helpers (`html_to_text`, `extract_numbered_list`) before being written out.
