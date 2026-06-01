import pandas as pd
from pathlib import Path


def generate_endpoint_file(csv_path: str):
    file_path = Path(csv_path)

    df = pd.read_csv(file_path)

    endpoint_df = df[["ID", "Testrail Link"]].copy()

    endpoint_df["testrail_id"] = (
        endpoint_df["Testrail Link"]
        .str.extract(r"/view/(\d+)")
    )

    endpoint_df["endpoint"] = (
        "https://myntfintech.testrail.io/index.php?/api/v2/get_case/"
        + endpoint_df["testrail_id"]
    )

    endpoint_df = endpoint_df[
        ["ID", "endpoint"]
    ]

    output_dir = Path("endpoints")
    output_dir.mkdir(exist_ok=True)

    output_file = (
        output_dir /
        f"{file_path.stem}-endpoint.csv"
    )

    endpoint_df.to_csv(
        output_file,
        index=False
    )

    return output_file
