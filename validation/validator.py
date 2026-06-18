import logging
import os
import time
from datetime import datetime
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
}

PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o",
    "google": "gemini-2.0-flash",
    "ollama": "llama3.3",
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

Return a structured evaluation with per-field results and an overall verdict.\
"""


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
    overall_passed: bool = Field(description="True only if all three criteria pass")
    summary: str = Field(description="One-sentence overall quality assessment")


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


def validate_cases(
    passed_cases: list[dict],
    output_dir: str = "output",
    provider: str = "anthropic",
    model: str | None = None,
    delay: float = 0,
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

    logger.info("Using provider=%s model=%s", provider, model)

    requirements = _load_requirements()
    chain = _build_chain(provider, model)

    rows = []
    total = len(passed_cases)

    for i, case in enumerate(passed_cases, 1):
        case_id = case.get("Test Case ID", "")
        logger.info("[%d/%d] Validating %s", i, total, case_id)

        try:
            result: CaseValidationResult = chain.invoke({
                "requirements": requirements,
                "test_case_id": case_id,
                "title": case.get("Title", "") or "",
                "testing_type": case.get("Testing Type", "") or "",
                "testing_phase": case.get("Testing Phase", "") or "",
                "preconditions": case.get("Pre Conditions", "") or "(empty)",
                "test_steps": case.get("Test Steps", "") or "(empty)",
                "expected_result": case.get("Expected Result", "") or "(empty)",
            })

            rows.append({
                "Test Case ID": result.test_case_id,
                "Title": case.get("Title", ""),
                "Preconditions Passed": result.preconditions.passed,
                "Preconditions Remarks": result.preconditions.remarks,
                "Test Steps Passed": result.test_steps.passed,
                "Test Steps Remarks": result.test_steps.remarks,
                "Expected Results Passed": result.expected_results.passed,
                "Expected Results Remarks": result.expected_results.remarks,
                "Overall Passed": result.overall_passed,
                "Summary": result.summary,
            })

        except Exception as e:
            logger.error("Validation failed for %s: %s", case_id, e, exc_info=True)
            rows.append({
                "Test Case ID": case_id,
                "Title": case.get("Title", ""),
                "Preconditions Passed": None,
                "Preconditions Remarks": f"Validation error: {e}",
                "Test Steps Passed": None,
                "Test Steps Remarks": "",
                "Expected Results Passed": None,
                "Expected Results Remarks": "",
                "Overall Passed": None,
                "Summary": f"Error during validation: {e}",
            })

        if delay > 0 and i < total:
            time.sleep(delay)

    df = pd.DataFrame(rows)

    out_dir = Path(output_dir) / "llm_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = out_dir / f"llm_validation_{timestamp}.csv"
    df.to_csv(output_file, index=False, encoding="utf-8-sig")

    passed_count = df["Overall Passed"].eq(True).sum()
    failed_count = df["Overall Passed"].eq(False).sum()
    error_count = df["Overall Passed"].isna().sum()
    logger.info(
        "Validation complete: %d cases -> %s (Passed: %d, Failed: %d, Errors: %d)",
        total, output_file, passed_count, failed_count, error_count,
    )

    return output_file
