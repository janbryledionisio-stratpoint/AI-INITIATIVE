import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

AUTOMATABILITY_MAP = {
    1: "Automated",
    2: "Automatable",
    3: "Non-Automatable",
    4: "To be Determined",
    5: "For Migration"
}

AUTOMATION_STATUS_MAP = {
    1: "Not Started",
    2: "In Progress",
    3: "Blocked Test Data",
    4: "Blocked Incomplete Steps",
    5: "Blocked Access Issues",
    6: "Obsolete",
    7: "Blocked Feature Issue",
    8: "Blocked Others",
    9: "Reviewed = Passed DOR",
    10: "Completed",
    11: "None"
}

TESTING_TYPE_MAP = {
    1: "api",
    2: "api-and-database",
    3: "api-and-mobile",
    4: "api-and-logs-checking",
    5: "api-and-sms",
    6: "api-and-web",
    7: "database",
    8: "database-and-logs-checking",
    9: "database-and-sms",
    10: "logs-checking",
    11: "mobile",
    12: "mobile-and-database",
    13: "mobile-and-logs-checking",
    14: "mobile-and-sms",
    15: "sms",
    16: "sms-and-logs-checking",
    17: "web",
    18: "web-and-database",
    19: "web-and-logs-checking",
    20: "web-and-mobile",
    21: "web-and-sms"
}

STATUS_MAP = {
    2: "New",
    3: "Reviewed",
    4: "Obsolete",
    5: "Inactive",
    6: "Active"
}

TESTING_PHASE_MAP = {
    1: "BE Regression",
    2: "FE Regression",
    3: "FVT",
    4: "FE Sanity",
    5: "BE Sanity",
    6: "Pre-Test",
    7: "Post-Test",
    8: "Cleanpass Project",
    9: "Sprint Testing",
    10: "Sanity",
    11: "Smoke",
    12: "Regression"
}


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
        step_block = tc["custom_steps_separated"][0]
        steps_html = step_block.get("content", "")
        expected_html = step_block.get("expected", "")

    return {
        "General Standards": standard_result,
        "Remarks": remarks,
        "Test Case ID": f"C{case_id}",
        "Title": tc.get("title"),
        "Project Initiative": html_to_text(tc.get("custom_project_initiative")),
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

    if tc.get("custom_automatability") not in [2, 4]:
        failures.append(
            f"Automatability = {AUTOMATABILITY_MAP.get(tc.get('custom_automatability'))}"
        )

    if tc.get("custom_testing_type") != 17:
        failures.append(
            f"Testing Type = {TESTING_TYPE_MAP.get(tc.get('custom_testing_type'))}"
        )

    if tc.get("custom_automation_status") != 1:
        failures.append(
            f"Automation Status = {AUTOMATION_STATUS_MAP.get(tc.get('custom_automation_status'))}"
        )

    if tc.get("case_status_id") not in [2, 3, 6]:
        failures.append(
            f"Status = {STATUS_MAP.get(tc.get('case_status_id'))}"
        )

    if tc.get("priority_id") is None:
        failures.append("Priority/Risk is null")

    phases = tc.get("custom_testing_phase", [])
    if not any(phase in [1, 2, 12] for phase in phases):
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
