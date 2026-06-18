# Initiative Requirements — Web MVP

Single source of truth for the requirements of this AI initiative (Web MVP scope).
Edit this file when requirements change. The general standards filter criteria are
maintained in [pipeline/filter.py](pipeline/filter.py) (`evaluate_case()`).
The **LLM evaluates §3.2 – §3.4 only** — §2 criteria are enforced upstream.

> Status: living document. Being assembled from AF Team discussion notes; sections
> will be appended as further notes arrive.
>
> **MVP scope: Web test cases only.** Mobile, API, Database, and Hybrid support are
> out of scope for this iteration.

---

## 1. Initiative Overview

An AI initiative built around two agents:

1. **TC Analyzer Agent** — analyzes whether a web test case meets quality standards
   and is feasible to automate.
2. **Automation Script Generation Agent** — generates web automation scripts from
   validated test cases, bounded by predefined SOPs. *(out of scope for MVP)*

**Core workflow:** `Tribe → TC Analyzer → Automation Script Gen → Web`

### Objectives
- Reduce manual scripting effort
- Catch incomplete or invalid test cases early
- Speed up automation backlog resolution
- Standardize automation processes across tribes

### MVP focus
- TC Analyzer is the **POC / "quick win"**, targeted at Web test cases.
- **Cursor** is the primary AI development tool.
- CSV files are used for data ingestion.

### TC Analyzer scope (Web MVP)
Validates web test case quality against §3 standards below. Input is a CSV export
from TestRail; cases that fail the §2 general standards pre-filter are excluded
before LLM evaluation runs.

---

## 2. General Standards (pre-filtered — not evaluated by LLM)

All cases that reach LLM validation have **already passed** the general standards
filter enforced by `evaluate_case()` in `pipeline/filter.py`. The criteria below are
guaranteed satisfied — the LLM does not need to re-check them.

| Criterion | Required value |
|---|---|
| Automatability | Automatable or To be Determined |
| Testing Type | Web |
| Automation Status | Not Started |
| Status | New, Reviewed, or Active |
| Priority | Must not be null |
| Testing Phase | Must include BE Regression, FE Regression, or Regression |

---

## 3. Test Case Quality Standards (Web)

These describe what a "clean" web test case looks like for reliable automation.
The LLM evaluates §3.2 – §3.4 only.

### 3.1 Title
- Title must clearly describe the business objective or user outcome.
- Title must include the platform label **Web**.
- Test case must **not** be in an obsolete, inactive, or archive folder.

### 3.2 Preconditions
Web test cases must satisfy all three of the following:
- **URL** — the specific URL or page the test starts from; acceptable in either the
  preconditions or the first applicable test step.
- **Credentials** — username/password, role, or account type used during the test
  (e.g. "LDAP account", "Fund Recovery - Maker role"); must be identifiable from the
  preconditions.
- **Environment** — the target environment; may be stated explicitly (e.g. "SIT",
  "UAT") or implied by the URL or account name (e.g. `buservice-sit` in the URL or
  account description is sufficient).

### 3.3 Test Steps
- Steps must be **actionable / atomic** — one user action per step.
- Steps must use **imperative wording** (e.g. "Click", "Enter", "Navigate").
- For every input/enter/type/insert action, the **exact data to be used must be specified**.
- The test case must have **at least one expected result** covering the main test
  objective. A single summary expected result at the end is acceptable; per-step
  expected results are preferred but not required.

### 3.4 Expected Results
- Must be **explicitly defined** — not implied or left to interpretation.
- Must be **objective and measurable** — each assertion verifies one logical behavior.
- May use **conditional language** (e.g. "if the table is too long, it should be
  scrollable") provided the condition and expected outcome are both clearly stated.
