import logging
import re
from datetime import datetime
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


def _ts_from_path(path: Path) -> datetime:
    m = re.search(r"(\d{8}_\d{6})", path.name)
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") if m else datetime.min


def get_latest_output_file(output_dir: str = "output") -> Path:
    out_dir = Path(output_dir) / "testcases_with_standards"
    files = list(out_dir.glob("testcases_with_standards_*.csv"))
    if not files:
        raise FileNotFoundError(f"No testcases_with_standards_*.csv found in {out_dir}/")
    return max(files, key=_ts_from_path)


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
