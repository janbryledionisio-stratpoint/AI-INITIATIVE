import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_COLS = {
    "General Standards",
    "Test Case ID",
    "Title",
    "Testing Type",
    "Testing Phase",
    "Pre Conditions",
    "Test Steps",
    "Expected Result",
}


def get_latest_output_file(output_dir: str = "output") -> Path:
    out_dir = Path(output_dir) / "testcases_with_standards"
    files = sorted(out_dir.glob("testcases_with_standards_*.csv"))
    if not files:
        raise FileNotFoundError(f"No testcases_with_standards_*.csv found in {out_dir}/")
    return files[-1]


def fetch_passed_cases(output_dir: str = "output") -> list[dict]:
    latest_file = get_latest_output_file(output_dir)
    logger.info("Loading from: %s", latest_file)

    df = pd.read_csv(latest_file)

    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"Output CSV missing required columns: {missing}")

    passed = df[df["General Standards"] == "Passed"].copy()
    logger.info(
        "Found %d passed test cases (out of %d total)", len(passed), len(df)
    )
    return passed.to_dict(orient="records")
