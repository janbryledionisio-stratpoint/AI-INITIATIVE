import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_MAX_WORKERS = 5


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _fetch_one(session: requests.Session, row: dict, username: str, api_key: str) -> dict:
    endpoint = str(row.get("endpoint", "")).strip()
    logger.info("Fetching: %s", endpoint)
    try:
        response = session.get(endpoint, auth=(username, api_key), timeout=30)
        logger.debug("Status: %d for %s", response.status_code, endpoint)
        return {
            "ID": row["ID"],
            "status_code": response.status_code,
            "response": response.json() if response.ok else None,
        }
    except requests.exceptions.RequestException as e:
        logger.warning("Request failed for %s: %s", endpoint, e)
        return {"ID": row["ID"], "status_code": None, "response": None}
    except Exception as e:
        logger.error("Unexpected error for %s: %s", endpoint, e, exc_info=True)
        return {"ID": row["ID"], "status_code": None, "response": None}


def fetch_test_cases(endpoint_csv) -> list[dict]:
    df = pd.read_csv(endpoint_csv)

    username = os.getenv("TESTRAIL_USERNAME")
    api_key = os.getenv("TESTRAIL_PASSWORD")

    if not username or not api_key:
        raise EnvironmentError(
            "TESTRAIL_USERNAME and TESTRAIL_PASSWORD must be set in environment"
        )

    session = _build_session()
    rows = df.to_dict(orient="records")

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = [
            executor.submit(_fetch_one, session, row, username, api_key)
            for row in rows
        ]
        results = [f.result() for f in futures]

    failed = sum(1 for r in results if r["status_code"] is None)
    if failed:
        logger.warning("%d/%d requests failed", failed, len(rows))

    return results
