import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_FIELD_MAPS_PATH = Path(__file__).parent / "field_maps.json"


def _load_maps() -> tuple[dict, dict]:
    with open(_FIELD_MAPS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    field_maps = {
        name: {int(k): v for k, v in mapping.items()}
        for name, mapping in data["field_maps"].items()
    }
    retrieval_maps = {
        name: {int(k): v for k, v in mapping.items() if k != "_comment"}
        for name, mapping in data["retrieval_fields"].items()
        if not name.startswith("_")
    }
    return field_maps, retrieval_maps


_field_maps, _retrieval_maps = _load_maps()

AUTOMATABILITY_MAP: dict[int, str] = _field_maps["AUTOMATABILITY_MAP"]
AUTOMATION_STATUS_MAP: dict[int, str] = _field_maps["AUTOMATION_STATUS_MAP"]
TESTING_TYPE_MAP: dict[int, str] = _field_maps["TESTING_TYPE_MAP"]
STATUS_MAP: dict[int, str] = _field_maps["STATUS_MAP"]
TESTING_PHASE_MAP: dict[int, str] = _field_maps["TESTING_PHASE_MAP"]

TRIBE_MAP: dict[int, str] = _retrieval_maps.get("TRIBE_MAP", {})
SUB_MODULE_MAP: dict[int, str] = _retrieval_maps.get("SUB_MODULE_MAP", {})
PRODUCT_MAP: dict[int, str] = _retrieval_maps.get("PRODUCT_MAP", {})
CAPABILITY_MAP: dict[int, str] = _retrieval_maps.get("CAPABILITY_MAP", {})

# Allowed IDs for each General Standards criterion, derived from the maps so that
# changes in field_maps.json automatically propagate here.
_PASS_AUTOMATABILITY: frozenset[int] = frozenset(
    k for k, v in AUTOMATABILITY_MAP.items()
    if v in {"Automatable", "To be Determined"}
)
_PASS_TESTING_TYPE: frozenset[int] = frozenset(
    k for k, v in TESTING_TYPE_MAP.items()
    if v == "web"
)
_PASS_AUTOMATION_STATUS: frozenset[int] = frozenset(
    k for k, v in AUTOMATION_STATUS_MAP.items()
    if v in {"Not Started", "Reviewed = Passed DOR"}
)
_PASS_CASE_STATUS: frozenset[int] = frozenset(
    k for k, v in STATUS_MAP.items()
    if v in {"New", "Reviewed", "Active"}
)
_PASS_TESTING_PHASE: frozenset[int] = frozenset(
    k for k, v in TESTING_PHASE_MAP.items()
    if v in {"BE Regression", "FE Regression", "Regression"}
)


def map_get(mapping: dict[int, str], key: int | None) -> str:
    """Look up an integer ID in a map; both sides normalized to lowercase for comparison."""
    if key is None:
        return ""
    val = mapping.get(int(key), "")
    return val


def find_by_label(mapping: dict[int, str], label: str) -> int | None:
    """Reverse lookup by label, case-insensitive (lowercases both actual and expected)."""
    label_lower = label.lower()
    for k, v in mapping.items():
        if v.lower() == label_lower:
            return k
    return None


def html_to_text(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text("\n", strip=True)


def extract_numbered_list(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for idx, li in enumerate(soup.find_all("li"), start=1):
        text = li.get_text(" ", strip=True)
        items.append(f"{idx}. {text}")
    return "\n".join(items)


def transform_case(case_id, tc):
    standard_result, remarks = evaluate_case(tc)

    steps_html = ""
    expected_html = ""

    if tc.get("custom_steps_separated"):
        step_blocks = tc["custom_steps_separated"]
        steps_html = "\n".join(b.get("content", "") for b in step_blocks)
        expected_html = "\n".join(b.get("expected", "") for b in step_blocks)

    return {
        "General Standards": standard_result,
        "Remarks": remarks,
        "Test Case ID": f"C{case_id}",
        "Title": tc.get("title"),
        "Project Initiative": html_to_text(tc.get("custom_project_initiative")),
        "Tribe": TRIBE_MAP.get(tc.get("custom_case_tribe")),
        "Product": PRODUCT_MAP.get(tc.get("custom_product_prod")),
        "Capability": CAPABILITY_MAP.get(tc.get("custom_capability")),
        "Priority": tc.get("priority_id"),
        "Automatability": AUTOMATABILITY_MAP.get(tc.get("custom_automatability")),
        "Automation Status": AUTOMATION_STATUS_MAP.get(tc.get("custom_automation_status")),
        "Testing Type": TESTING_TYPE_MAP.get(tc.get("custom_testing_type")),
        "Status": STATUS_MAP.get(tc.get("case_status_id")),
        "Testing Phase": ", ".join(
            TESTING_PHASE_MAP.get(p, str(p))
            for p in tc.get("custom_testing_phase", [])
        ),
        "Pre Conditions": extract_numbered_list(tc.get("custom_preconds")),
        "Test Steps": extract_numbered_list(steps_html),
        "Expected Result": html_to_text(expected_html),
    }


def evaluate_case(tc):
    failures = []

    if tc.get("custom_automatability") not in _PASS_AUTOMATABILITY:
        failures.append(
            f"Automatability = {AUTOMATABILITY_MAP.get(tc.get('custom_automatability'))}"
        )

    if tc.get("custom_testing_type") not in _PASS_TESTING_TYPE:
        failures.append(
            f"Testing Type = {TESTING_TYPE_MAP.get(tc.get('custom_testing_type'))}"
        )

    if tc.get("custom_automation_status") not in _PASS_AUTOMATION_STATUS:
        failures.append(
            f"Automation Status = {AUTOMATION_STATUS_MAP.get(tc.get('custom_automation_status'))}"
        )

    if tc.get("case_status_id") not in _PASS_CASE_STATUS:
        failures.append(
            f"Status = {STATUS_MAP.get(tc.get('case_status_id'))}"
        )

    if tc.get("priority_id") is None:
        failures.append("Priority/Risk is null")

    phases = tc.get("custom_testing_phase", [])
    if not any(phase in _PASS_TESTING_PHASE for phase in phases):
        phase_names = [TESTING_PHASE_MAP.get(p, str(p)) for p in phases]
        failures.append(f"Testing Phase = {', '.join(phase_names)}")

    if failures:
        return "Failed", "; ".join(failures)

    return "Passed", ""


def save_all_cases(results: list[dict], output_dir: str = "output") -> Path:
    rows = []

    for result in results:
        if result["status_code"] != 200:
            continue
        tc = result["response"]
        rows.append(transform_case(tc["id"], tc))

    if not rows:
        logger.warning("No valid test cases to save")
        return None

    df = pd.DataFrame(rows)
    df["sort_order"] = df["General Standards"].map({"Passed": 0, "Failed": 1})
    df = df.sort_values(by=["sort_order", "Test Case ID"])
    df.drop(columns=["sort_order"], inplace=True)

    out_dir = Path(output_dir) / "testcases_with_standards"
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = out_dir / f"testcases_with_standards_{timestamp}.csv"

    df.to_csv(output_file, index=False, encoding="utf-8-sig")

    passed_count = (df["General Standards"] == "Passed").sum()
    failed_count = (df["General Standards"] == "Failed").sum()

    logger.info(
        "Saved %d test cases to %s (Passed: %d, Failed: %d)",
        len(df), output_file, passed_count, failed_count,
    )

    return output_file
