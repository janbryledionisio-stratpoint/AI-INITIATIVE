import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

TESTRAIL_BASE_URL = "https://myntfintech.testrail.io/index.php?/api/v2/get_case/"


def generate_endpoint_file(csv_path: str) -> Path:
    file_path = Path(csv_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    df = pd.read_csv(file_path)

    required_cols = {"ID", "Testrail Link"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Input CSV missing required columns: {missing}")

    endpoint_df = df[["ID", "Testrail Link"]].copy()
    endpoint_df["testrail_id"] = endpoint_df["Testrail Link"].str.extract(r"/view/(\d+)")

    bad_rows = endpoint_df["testrail_id"].isna()
    if bad_rows.any():
        logger.warning(
            "%d row(s) skipped — no /view/<id> found in Testrail Link: IDs %s",
            bad_rows.sum(),
            endpoint_df.loc[bad_rows, "ID"].tolist(),
        )
        endpoint_df = endpoint_df.dropna(subset=["testrail_id"])

    endpoint_df["endpoint"] = TESTRAIL_BASE_URL + endpoint_df["testrail_id"]
    endpoint_df = endpoint_df[["ID", "endpoint"]]

    output_dir = Path("endpoints")
    output_dir.mkdir(exist_ok=True)

    output_file = output_dir / f"{file_path.stem}-endpoint.csv"
    endpoint_df.to_csv(output_file, index=False)

    logger.info("Generated %d endpoints -> %s", len(endpoint_df), output_file)
    return output_file
