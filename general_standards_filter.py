import re
import pandas as pd
from bs4 import BeautifulSoup


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


def is_valid_case(tc):

    automatability = tc.get("custom_automatability")
    testing_type = tc.get("custom_testing_type")
    automation_status = tc.get("custom_automation_status")
    status = tc.get("case_status_id")
    priority = tc.get("priority_id")
    phases = tc.get("custom_testing_phase", [])

    return (
        automatability in [2, 4]
        and testing_type == 17
        and automation_status in [1, 9]
        and status in [2, 3, 6]
        and priority is not None
        and any(p in [1, 2, 12] for p in phases)
    )

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

        "Project Initiative":
            html_to_text(
                tc.get("custom_project_initiative")
            ),

        "Priority":
            tc.get("priority_id"),

        "Automatability":
            AUTOMATABILITY_MAP.get(
                tc.get("custom_automatability")
            ),

        "Automation Status":
            AUTOMATION_STATUS_MAP.get(
                tc.get("custom_automation_status")
            ),

        "Testing Type":
            TESTING_TYPE_MAP.get(
                tc.get("custom_testing_type")
            ),

        "Status":
            STATUS_MAP.get(
                tc.get("case_status_id")
            ),

        "Testing Phase":
            ", ".join(
                TESTING_PHASE_MAP.get(p, str(p))
                for p in tc.get(
                    "custom_testing_phase",
                    []
                )
            ),

        "Pre Conditions":
            extract_numbered_list(
                tc.get("custom_preconds")
            ),

        "Test Steps":
            extract_numbered_list(
                steps_html
            ),

        "Expected Result":
            html_to_text(
                expected_html
            )
    }

def save_all_cases(results):

    rows = []

    for result in results:

        if result["status_code"] != 200:
            continue

        tc = result["response"]

        rows.append(
            transform_case(
                tc["id"],
                tc
            )
        )

    df = pd.DataFrame(rows)

    # Passed first
    df["sort_order"] = df["General Standards"].map({
        "Passed": 0,
        "Failed": 1
    })

    df = df.sort_values(
        by=[
            "sort_order",
            "Test Case ID"
        ]
    )

    df.drop(
        columns=["sort_order"],
        inplace=True
    )

    output_file = "testcases_with_standards.csv"

    df.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig"
    )

    print(
        f"Saved {len(df)} test cases to {output_file}"
    )

    passed_count = len(
        df[df["General Standards"] == "Passed"]
    )

    failed_count = len(
        df[df["General Standards"] == "Failed"]
    )

    print(f"Passed: {passed_count}")
    print(f"Failed: {failed_count}")

    return output_file



def evaluate_case(tc):

    failures = []

    # Automatability
    if tc.get("custom_automatability") not in [2, 4]:
        failures.append(
            f"Automatability = "
            f"{AUTOMATABILITY_MAP.get(tc.get('custom_automatability'))}"
        )

    # Testing Type
    if tc.get("custom_testing_type") != 17:
        failures.append(
            f"Testing Type = "
            f"{TESTING_TYPE_MAP.get(tc.get('custom_testing_type'))}"
        )

    # Automation Status
    if tc.get("custom_automation_status") not in [1, 9]:
        failures.append(
            f"Automation Status = "
            f"{AUTOMATION_STATUS_MAP.get(tc.get('custom_automation_status'))}"
        )

    # Status
    if tc.get("case_status_id") not in [2, 3, 6]:
        failures.append(
            f"Status = "
            f"{STATUS_MAP.get(tc.get('case_status_id'))}"
        )

    # Priority
    if tc.get("priority_id") is None:
        failures.append(
            "Priority/Risk is null"
        )

    # Testing Phase
    phases = tc.get(
        "custom_testing_phase",
        []
    )

    if not any(
        phase in [1, 2, 12]
        for phase in phases
    ):
        phase_names = [
            TESTING_PHASE_MAP.get(p, str(p))
            for p in phases
        ]

        failures.append(
            f"Testing Phase = "
            f"{', '.join(phase_names)}"
        )

    if failures:
        return (
            "Failed",
            "; ".join(failures)
        )

    return (
        "Passed",
        ""
    )


