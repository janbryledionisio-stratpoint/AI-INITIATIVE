import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from enum import Enum
from pathlib import Path

import pandas as pd
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

REQUIREMENTS_PATH = Path("requirements-web.md")

PROVIDER_ENV_VAR: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "ollama": None,
    "claude-code": None,  # uses Claude Code CLI authentication
}

PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "ollama": "llama3.3",
    "claude-code": "claude-code",
}

SYSTEM_PROMPT = (
    "You are a QA standards evaluator. "
    "Evaluate test cases strictly against the documented quality standards provided. "
    "Be specific about issues — quote exact text where possible. "
    "Keep remarks concise."
)

HUMAN_TEMPLATE = """\
## Quality Standards Reference

{requirements}

---
{few_shot_context}
## Test Case to Evaluate

**Test Case ID:** {test_case_id}
**Title:** {title}
**Testing Type:** {testing_type}
**Testing Phase:** {testing_phase}

**Pre Conditions:**
{preconditions}

**Test Steps:**
{test_steps}

**Expected Result:**
{expected_result}

---

Evaluate this Web test case against the quality standards above:

- **Pre Conditions** (§3.2 Web): URL must appear in preconditions or the first applicable \
test step. Credentials (username/password, role, or account type) must be identifiable from \
preconditions. Environment may be explicit ("SIT") or implied by the URL or account name.
- **Test Steps** (§3.3): each step must be atomic (one action only), use imperative wording, \
and specify exact data for every input/type/enter action. At least one expected result covering \
the main test objective is required; a single summary expected result is acceptable.
- **Expected Result** (§3.4): must be explicitly defined, objective, and measurable. \
Conditional language (e.g. "if X, then Y") is acceptable when the condition and outcome are clear.

**Verdict rules** — choose exactly one:
- **Passed**: ALL three criteria are fully satisfied.
- **Needs Improvement**: content is present across all criteria but one or more have quality \
gaps (e.g. steps not fully atomic, input data not fully specified, credentials implied but \
not explicitly stated, expected result partially measurable). The test case is salvageable \
with targeted edits.
- **Failed**: one or more criteria are fundamentally broken — required content is entirely \
absent (no URL or credentials anywhere, no test steps at all, no expected result defined).

Return a structured evaluation with per-criterion results, an overall verdict, \
a cross-cutting summary of issues found (`overall_remarks`), and specific actionable \
suggestions to reach Passed (`improvements`).\
"""


def _format_few_shots(examples: list[dict]) -> str:
    """Format DB few-shot examples into a prompt section.

    Returns an empty string when there are no examples so the template
    placeholder collapses cleanly.
    """
    if not examples:
        return ""

    lines = [
        "\n## Reference Examples (similar validated cases — for calibration only)\n",
    ]
    for i, ex in enumerate(examples, 1):
        verdict = ex.get("verdict") or "ERROR"
        pre_icon   = "✓" if ex.get("preconditions_passed")    == 1 else "✗"
        steps_icon = "✓" if ex.get("test_steps_passed")       == 1 else "✗"
        exp_icon   = "✓" if ex.get("expected_results_passed") == 1 else "✗"

        ctx = ex.get("capability") or ex.get("product") or ex.get("tribe") or ""
        lines += [
            f"### Example {i} — {ex.get('case_id', '')} [{verdict.upper()}]  _(context: {ctx})_",
            f"**Title:** {ex.get('title', '')}",
            "",
            "**Pre Conditions:**",
            ex.get("pre_conditions") or "(empty)",
            "",
            "**Test Steps:**",
            ex.get("test_steps") or "(empty)",
            "",
            "**Expected Result:**",
            ex.get("expected_result") or "(empty)",
            "",
            "**Evaluation:**",
            f"- Pre Conditions: {pre_icon}"
            + (f" — {ex['preconditions_remarks']}" if ex.get("preconditions_remarks") else ""),
            f"- Test Steps: {steps_icon}"
            + (f" — {ex['test_steps_remarks']}" if ex.get("test_steps_remarks") else ""),
            f"- Expected Results: {exp_icon}"
            + (f" — {ex['expected_results_remarks']}" if ex.get("expected_results_remarks") else ""),
            f"- **Verdict: {verdict}**",
        ]
        if ex.get("overall_remarks"):
            lines.append(f"- Overall Remarks: {ex['overall_remarks']}")
        if ex.get("improvements"):
            lines.append(f"- Improvements: {ex['improvements']}")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


class Verdict(str, Enum):
    passed = "Passed"
    needs_improvement = "Needs Improvement"
    failed = "Failed"


class CriterionResult(BaseModel):
    passed: bool = Field(description="True if this criterion is fully satisfied")
    remarks: str = Field(description="Specific issues found, or empty string if passed")


class CaseValidationResult(BaseModel):
    test_case_id: str = Field(description="The Test Case ID (e.g. C1234567)")
    preconditions: CriterionResult = Field(
        description="Evaluation of Pre Conditions against §3.2 (Web)"
    )
    test_steps: CriterionResult = Field(
        description="Evaluation of Test Steps against §3.3"
    )
    expected_results: CriterionResult = Field(
        description="Evaluation of Expected Result against §3.4"
    )
    verdict: Verdict = Field(
        description=(
            "Passed — all criteria fully met. "
            "Needs Improvement — content present but quality gaps exist (salvageable). "
            "Failed — required content is entirely absent in one or more criteria."
        )
    )
    overall_remarks: str = Field(
        description="Cross-cutting summary of all issues found across the three criteria"
    )
    improvements: str = Field(
        description="Specific, actionable steps the test-case author should take to reach Passed"
    )


def _load_requirements() -> str:
    if not REQUIREMENTS_PATH.exists():
        raise FileNotFoundError(f"Requirements file not found: {REQUIREMENTS_PATH}")
    return REQUIREMENTS_PATH.read_text(encoding="utf-8")


def _build_llm(provider: str, model: str):
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=0)
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=0)
    elif provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, temperature=0)
    elif provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, temperature=0)
    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Choose from: {', '.join(PROVIDER_DEFAULT_MODEL)}"
        )


def _build_chain(provider: str, model: str):
    llm = _build_llm(provider, model)
    structured_llm = llm.with_structured_output(CaseValidationResult)
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", HUMAN_TEMPLATE),
    ])
    return prompt | structured_llm


def _extract_json(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"No JSON object found in response: {text[:300]}")


def _safe_str(val) -> str:
    """Convert a value to string, treating None and NaN as empty string."""
    if val is None:
        return ""
    if isinstance(val, float) and val != val:  # NaN != NaN
        return ""
    return str(val)


def _build_claude_code_prompt(requirements: str, case: dict, few_shot_context: str = "") -> str:
    case_id = _safe_str(case.get("Test Case ID", ""))
    preconditions = _safe_str(case.get("Pre Conditions", "")) or "(empty)"
    test_steps = _safe_str(case.get("Test Steps", "")) or "(empty)"
    expected_result = _safe_str(case.get("Expected Result", "")) or "(empty)"
    lines = [
        SYSTEM_PROMPT,
        "",
        "## Quality Standards Reference",
        "",
        requirements,
        "",
        "---",
        few_shot_context,
        "## Test Case to Evaluate",
        "",
        f"**Test Case ID:** {case_id}",
        f"**Title:** {_safe_str(case.get('Title', ''))}",
        f"**Testing Type:** {_safe_str(case.get('Testing Type', ''))}",
        f"**Testing Phase:** {_safe_str(case.get('Testing Phase', ''))}",
        "",
        "**Pre Conditions:**",
        preconditions,
        "",
        "**Test Steps:**",
        test_steps,
        "",
        "**Expected Result:**",
        expected_result,
        "",
        "---",
        "",
        "Evaluate this Web test case against §3.2–§3.4 of the quality standards above:",
        "- Pre Conditions (§3.2): URL in preconditions or first step; credentials (role/account type OK); environment explicit or implied by URL/account name.",
        "- Test Steps (§3.3): atomic, imperative wording, exact data for inputs, at least one expected result covering the main objective.",
        "- Expected Result (§3.4): explicitly defined, measurable; conditional language OK if condition and outcome are clear.",
        "",
        "Return ONLY a valid JSON object — no markdown, no explanation:",
        f'{{"test_case_id": "{case_id}", "preconditions": {{"passed": true, "remarks": ""}}, "test_steps": {{"passed": true, "remarks": ""}}, "expected_results": {{"passed": true, "remarks": ""}}, "verdict": "Passed", "overall_remarks": "cross-cutting summary of issues", "improvements": "actionable steps to reach Passed"}}',
    ]
    return "\n".join(lines)


def _validate_one_with_claude_code(requirements: str, case: dict, few_shot_context: str = "") -> CaseValidationResult:
    case_id = case.get("Test Case ID", "")
    prompt = _build_claude_code_prompt(requirements, case, few_shot_context)

    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"Claude Code CLI error: {proc.stderr.strip()}")

    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"Claude Code returned error: {envelope.get('result', '')}")

    data = _extract_json(envelope["result"])

    return CaseValidationResult(
        test_case_id=data.get("test_case_id", case_id),
        preconditions=CriterionResult(**data["preconditions"]),
        test_steps=CriterionResult(**data["test_steps"]),
        expected_results=CriterionResult(**data["expected_results"]),
        verdict=data.get("verdict", "Failed"),
        overall_remarks=data.get("overall_remarks", ""),
        improvements=data.get("improvements", ""),
    )


def validate_cases(
    passed_cases: list[dict],
    output_dir: str = "output",
    provider: str = "anthropic",
    model: str | None = None,
    delay: float = 0,
    db_path: "Path | None" = None,
    run_id: str | None = None,
    max_workers: int = 1,
) -> Path:
    if provider not in PROVIDER_DEFAULT_MODEL:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Choose from: {', '.join(PROVIDER_DEFAULT_MODEL)}"
        )

    if model is None:
        model = PROVIDER_DEFAULT_MODEL[provider]

    env_var = PROVIDER_ENV_VAR[provider]
    if env_var and not os.getenv(env_var):
        raise EnvironmentError(
            f"{env_var} must be set in environment for provider '{provider}'"
        )

    use_claude_code = provider == "claude-code"
    if use_claude_code:
        if shutil.which("claude") is None:
            raise EnvironmentError(
                "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
            )
        logger.warning(
            "claude-code provider shells out to the local Claude Code CLI; "
            "authentication must already be configured for the current user "
            "and will fail in headless/CI environments."
        )

    logger.info("Using provider=%s model=%s", provider, model)

    requirements = _load_requirements()
    chain = None if use_claude_code else _build_chain(provider, model)

    # Resolve DB path once so few-shot retrieval and incremental saves use the same file
    _db_path: "Path | None" = None
    try:
        from database import DB_DEFAULT_PATH
        _db_path = Path(db_path) if db_path else DB_DEFAULT_PATH
    except Exception:
        pass

    # Generate (or reuse) the run_id — doubles as the output filename timestamp
    timestamp = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    # Init the DB run row and discover already-completed cases (for crash resume)
    skipped_ids: set[str] = set()
    if _db_path is not None:
        try:
            from database import init_validation_run, get_completed_case_ids
            init_validation_run(timestamp, provider, model, len(passed_cases), db_path=_db_path)
            if run_id:
                skipped_ids = get_completed_case_ids(run_id, db_path=_db_path)
                if skipped_ids:
                    logger.info(
                        "Resuming run %s — skipping %d already-validated case(s)",
                        timestamp, len(skipped_ids),
                    )
        except Exception as e:
            logger.debug("DB init skipped: %s", e)

    total = len(passed_cases)
    _db_lock = threading.Lock()

    def _process_case(i: int, case: dict) -> dict | None:
        case_id = case.get("Test Case ID", "")

        if case_id in skipped_ids:
            logger.info("[%d/%d] Skipping %s (already validated)", i, total, case_id)
            return None

        logger.info("[%d/%d] Validating %s", i, total, case_id)

        few_shot_context = ""
        if _db_path is not None:
            try:
                from database import get_few_shots
                shots = get_few_shots(case, db_path=_db_path)
                few_shot_context = _format_few_shots(shots)
                if shots:
                    logger.info("  %d few-shot example(s) injected for %s", len(shots), case_id)
            except Exception as fs_err:
                logger.debug("Few-shot retrieval skipped for %s: %s", case_id, fs_err)

        try:
            if use_claude_code:
                result: CaseValidationResult = _validate_one_with_claude_code(
                    requirements, case, few_shot_context
                )
            else:
                result = chain.invoke({
                    "requirements": requirements,
                    "few_shot_context": few_shot_context,
                    "test_case_id": case_id,
                    "title": case.get("Title", "") or "",
                    "testing_type": case.get("Testing Type", "") or "",
                    "testing_phase": case.get("Testing Phase", "") or "",
                    "preconditions": case.get("Pre Conditions", "") or "(empty)",
                    "test_steps": case.get("Test Steps", "") or "(empty)",
                    "expected_result": case.get("Expected Result", "") or "(empty)",
                })

            row = {
                "Test Case ID": result.test_case_id,
                "Title": case.get("Title", ""),
                "Preconditions Passed": result.preconditions.passed,
                "Preconditions Remarks": result.preconditions.remarks,
                "Test Steps Passed": result.test_steps.passed,
                "Test Steps Remarks": result.test_steps.remarks,
                "Expected Results Passed": result.expected_results.passed,
                "Expected Results Remarks": result.expected_results.remarks,
                "Verdict": result.verdict.value,
                "Overall Remarks": result.overall_remarks,
                "Improvements": result.improvements,
            }

        except Exception as e:
            logger.error("Validation failed for %s: %s", case_id, e, exc_info=True)
            row = {
                "Test Case ID": case_id,
                "Title": case.get("Title", ""),
                "Preconditions Passed": None,
                "Preconditions Remarks": f"Validation error: {e}",
                "Test Steps Passed": None,
                "Test Steps Remarks": "",
                "Expected Results Passed": None,
                "Expected Results Remarks": "",
                "Verdict": None,
                "Overall Remarks": f"Error during validation: {e}",
                "Improvements": "",
            }

        if _db_path is not None:
            try:
                from database import save_case_result
                with _db_lock:
                    save_case_result(case, row, timestamp, db_path=_db_path)
            except Exception as db_err:
                logger.warning("Incremental DB save failed for %s (non-fatal): %s", case_id, db_err)

        # delay only makes sense in sequential mode (rate-limit buffer per call)
        if delay > 0 and max_workers == 1:
            time.sleep(delay)

        return row

    rows: list[dict] = []
    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_process_case, i, case): i
                for i, case in enumerate(passed_cases, 1)
            }
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    rows.append(result)
    else:
        for i, case in enumerate(passed_cases, 1):
            row = _process_case(i, case)
            if row is not None:
                rows.append(row)

    df = pd.DataFrame(rows)

    out_dir = Path(output_dir) / "llm_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_file = out_dir / f"llm_validation_{timestamp}.csv"
    df.to_csv(output_file, index=False, encoding="utf-8-sig")

    passed_count = (df["Verdict"] == "Passed").sum()
    ni_count     = (df["Verdict"] == "Needs Improvement").sum()
    failed_count = (df["Verdict"] == "Failed").sum()
    error_count  = df["Verdict"].isna().sum()
    logger.info(
        "Validation complete: %d cases -> %s "
        "(Passed: %d, Needs Improvement: %d, Failed: %d, Errors: %d)",
        total, output_file, passed_count, ni_count, failed_count, error_count,
    )

    return output_file
