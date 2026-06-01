import pandas as pd
import requests
import os
from dotenv import load_dotenv

load_dotenv()


def fetch_test_cases(endpoint_csv):
    df = pd.read_csv(endpoint_csv)

    username = os.getenv("TESTRAIL_USERNAME")
    api_key = os.getenv("TESTRAIL_PASSWORD")

    print("Username:", username)
    print("Password exists:", api_key is not None)

    results = []

    for _, row in df.iterrows():

        endpoint = str(row["endpoint"]).strip()

        try:
            print(f"Calling: {endpoint}")

            response = requests.get(
                endpoint,
                auth=(username, api_key),
                timeout=30
            )

            print(f"Status: {response.status_code}")

            results.append({
                "ID": row["ID"],
                "status_code": response.status_code,
                "response": response.json()
                if response.ok
                else None
            })

        except Exception as e:
            print(f"Failed for {endpoint}")
            print(type(e).__name__)
            print(e)
            continue

    return results