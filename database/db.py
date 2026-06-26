"""SQLite persistence layer for the TC Analyzer pipeline.

Three tables:
  test_cases       — one row per unique case; content + categorisation fields
  validation_runs  — one row per LLM validation run (metadata)
  llm_validations  — per-case LLM results for every run (verdict, remarks, improvements)

verdict values: "Passed" | "Needs Improvement" | "Failed" | NULL (error during validation)
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_DEFAULT_PATH = Path("db/tc_analyzer.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS test_cases (
    case_id            TEXT PRIMARY KEY,
    title              TEXT,
    project_initiative TEXT,
    tribe              TEXT,
    product            TEXT,
    capability         TEXT,
    pre_conditions     TEXT,
    test_steps         TEXT,
    expected_result    TEXT,
    first_seen_at      TEXT NOT NULL,
    last_updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validation_runs (
    run_id                  TEXT PRIMARY KEY,
    provider                TEXT,
    model                   TEXT,
    total_cases             INTEGER,
    passed_count            INTEGER,
    needs_improvement_count INTEGER,
    failed_count            INTEGER,
    error_count             INTEGER,
    run_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_validations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id                  TEXT NOT NULL REFERENCES test_cases(case_id),
    run_id                   TEXT NOT NULL REFERENCES validation_runs(run_id),
    preconditions_passed     INTEGER,
    preconditions_remarks    TEXT,
    test_steps_passed        INTEGER,
    test_steps_remarks       TEXT,
    expected_results_passed  INTEGER,
    expected_results_remarks TEXT,
    verdict                  TEXT,
    overall_remarks          TEXT,
    improvements             TEXT,
    validated_at             TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema (safe to run on every init)."""
    existing_val = {
        row[1] for row in conn.execute("PRAGMA table_info(llm_validations)").fetchall()
    }
    for col, ddl in [
        ("verdict",          "ALTER TABLE llm_validations ADD COLUMN verdict TEXT"),
        ("overall_remarks",  "ALTER TABLE llm_validations ADD COLUMN overall_remarks TEXT"),
        ("improvements",     "ALTER TABLE llm_validations ADD COLUMN improvements TEXT"),
    ]:
        if col not in existing_val:
            conn.execute(ddl)

    existing_runs = {
        row[1] for row in conn.execute("PRAGMA table_info(validation_runs)").fetchall()
    }
    if "needs_improvement_count" not in existing_runs:
        conn.execute(
            "ALTER TABLE validation_runs ADD COLUMN needs_improvement_count INTEGER DEFAULT 0"
        )

    # Backfill verdict from overall_passed only when that legacy column exists
    if "overall_passed" in existing_val:
        conn.execute("""
            UPDATE llm_validations
            SET verdict = CASE
                WHEN overall_passed = 1 THEN 'Passed'
                WHEN overall_passed = 0 THEN 'Failed'
                ELSE NULL
            END
            WHERE verdict IS NULL
        """)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _to_int_or_none(val) -> int | None:
    """Normalise bool / numpy.bool_ / NaN / None → 1, 0, or NULL for SQLite."""
    if val is None:
        return None
    try:
        if isinstance(val, float) and val != val:  # NaN
            return None
        return int(bool(val))
    except (TypeError, ValueError):
        return None


def init_db(db_path: Path = DB_DEFAULT_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def init_validation_run(
    run_id: str,
    provider: str,
    model: str,
    total_cases: int,
    db_path: Path = DB_DEFAULT_PATH,
) -> None:
    """Create the validation_runs row at the very start of a run.

    Uses INSERT OR IGNORE so calling this on an already-started run (resume) is safe.
    """
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO validation_runs
                (run_id, provider, model, total_cases,
                 passed_count, needs_improvement_count, failed_count, error_count, run_at)
            VALUES (?,?,?,?,0,0,0,0,?)
            """,
            (run_id, provider, model, total_cases, now),
        )


def save_case_result(
    case: dict,
    row: dict,
    run_id: str,
    db_path: Path = DB_DEFAULT_PATH,
) -> None:
    """Persist one case result immediately after validation (crash-safe incremental write).

    Upserts the test_case row, inserts the llm_validations row, and recalculates
    the verdict counters on validation_runs so the totals stay current.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO test_cases (
                case_id, title, project_initiative,
                tribe, product, capability,
                pre_conditions, test_steps, expected_result,
                first_seen_at, last_updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(case_id) DO UPDATE SET
                title              = excluded.title,
                project_initiative = excluded.project_initiative,
                tribe              = excluded.tribe,
                product            = excluded.product,
                capability         = excluded.capability,
                pre_conditions     = excluded.pre_conditions,
                test_steps         = excluded.test_steps,
                expected_result    = excluded.expected_result,
                last_updated_at    = excluded.last_updated_at
            """,
            (
                case.get("Test Case ID", ""),
                case.get("Title"),
                case.get("Project Initiative"),
                case.get("Tribe"),
                case.get("Product"),
                case.get("Capability"),
                case.get("Pre Conditions"),
                case.get("Test Steps"),
                case.get("Expected Result"),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO llm_validations (
                case_id, run_id,
                preconditions_passed,    preconditions_remarks,
                test_steps_passed,       test_steps_remarks,
                expected_results_passed, expected_results_remarks,
                verdict, overall_remarks, improvements, validated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.get("Test Case ID", ""),
                run_id,
                _to_int_or_none(row.get("Preconditions Passed")),
                row.get("Preconditions Remarks"),
                _to_int_or_none(row.get("Test Steps Passed")),
                row.get("Test Steps Remarks"),
                _to_int_or_none(row.get("Expected Results Passed")),
                row.get("Expected Results Remarks"),
                row.get("Verdict"),
                row.get("Overall Remarks"),
                row.get("Improvements"),
                now,
            ),
        )
        conn.execute(
            """
            UPDATE validation_runs SET
                passed_count            = (
                    SELECT COUNT(*) FROM llm_validations
                    WHERE run_id = ? AND verdict = 'Passed'),
                needs_improvement_count = (
                    SELECT COUNT(*) FROM llm_validations
                    WHERE run_id = ? AND verdict = 'Needs Improvement'),
                failed_count            = (
                    SELECT COUNT(*) FROM llm_validations
                    WHERE run_id = ? AND verdict = 'Failed'),
                error_count             = (
                    SELECT COUNT(*) FROM llm_validations
                    WHERE run_id = ? AND verdict IS NULL)
            WHERE run_id = ?
            """,
            (run_id, run_id, run_id, run_id, run_id),
        )


def get_completed_case_ids(run_id: str, db_path: Path = DB_DEFAULT_PATH) -> set[str]:
    """Return the set of case_ids already persisted for a given run_id (for crash resume)."""
    if not db_path.exists():
        return set()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT case_id FROM llm_validations WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    return {r["case_id"] for r in rows}


def save_validation_run(
    passed_cases: list[dict],
    validation_rows: list[dict],
    run_id: str,
    provider: str,
    model: str,
    db_path: Path = DB_DEFAULT_PATH,
) -> None:
    """Persist one complete LLM validation run.

    Args:
        passed_cases:     Input dicts from the General Standards filter output
                          (fields: Test Case ID, Title, Tribe, Product, …).
        validation_rows:  Output dicts built by validate_cases() — one per case
                          with Overall Passed, per-criterion Passed/Remarks, Summary.
        run_id:           Timestamp string that becomes the primary key.
        provider/model:   LLM used for this run.
        db_path:          SQLite file path; parent directory is created automatically.
    """
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    passed_count = sum(1 for r in validation_rows if r.get("Verdict") == "Passed")
    ni_count     = sum(1 for r in validation_rows if r.get("Verdict") == "Needs Improvement")
    failed_count = sum(1 for r in validation_rows if r.get("Verdict") == "Failed")
    error_count  = sum(1 for r in validation_rows if r.get("Verdict") is None)

    with _connect(db_path) as conn:
        # ── test_cases (upsert — update content on repeat runs) ─────────────
        for case in passed_cases:
            conn.execute(
                """
                INSERT INTO test_cases (
                    case_id, title, project_initiative,
                    tribe, product, capability,
                    pre_conditions, test_steps, expected_result,
                    first_seen_at, last_updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(case_id) DO UPDATE SET
                    title              = excluded.title,
                    project_initiative = excluded.project_initiative,
                    tribe              = excluded.tribe,
                    product            = excluded.product,
                    capability         = excluded.capability,
                    pre_conditions     = excluded.pre_conditions,
                    test_steps         = excluded.test_steps,
                    expected_result    = excluded.expected_result,
                    last_updated_at    = excluded.last_updated_at
                """,
                (
                    case.get("Test Case ID", ""),
                    case.get("Title"),
                    case.get("Project Initiative"),
                    case.get("Tribe"),
                    case.get("Product"),
                    case.get("Capability"),
                    case.get("Pre Conditions"),
                    case.get("Test Steps"),
                    case.get("Expected Result"),
                    now,
                    now,
                ),
            )

        # ── validation_runs ──────────────────────────────────────────────────
        conn.execute(
            """
            INSERT OR REPLACE INTO validation_runs
                (run_id, provider, model, total_cases,
                 passed_count, needs_improvement_count, failed_count, error_count, run_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (run_id, provider, model, len(validation_rows),
             passed_count, ni_count, failed_count, error_count, now),
        )

        # ── llm_validations ──────────────────────────────────────────────────
        for row in validation_rows:
            conn.execute(
                """
                INSERT INTO llm_validations (
                    case_id, run_id,
                    preconditions_passed,    preconditions_remarks,
                    test_steps_passed,       test_steps_remarks,
                    expected_results_passed, expected_results_remarks,
                    verdict, overall_remarks, improvements, validated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.get("Test Case ID", ""),
                    run_id,
                    _to_int_or_none(row.get("Preconditions Passed")),
                    row.get("Preconditions Remarks"),
                    _to_int_or_none(row.get("Test Steps Passed")),
                    row.get("Test Steps Remarks"),
                    _to_int_or_none(row.get("Expected Results Passed")),
                    row.get("Expected Results Remarks"),
                    row.get("Verdict"),
                    row.get("Overall Remarks"),
                    row.get("Improvements"),
                    now,
                ),
            )


def get_all_cases(db_path: Path = DB_DEFAULT_PATH) -> list[dict]:
    """All test cases joined with their most recent LLM validation result."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                tc.case_id, tc.title, tc.tribe, tc.product, tc.capability,
                tc.project_initiative,
                tc.pre_conditions, tc.test_steps, tc.expected_result,
                tc.first_seen_at, tc.last_updated_at,
                lv.verdict,
                lv.preconditions_passed,    lv.preconditions_remarks,
                lv.test_steps_passed,       lv.test_steps_remarks,
                lv.expected_results_passed, lv.expected_results_remarks,
                lv.overall_remarks, lv.improvements, lv.validated_at,
                vr.provider, vr.model, vr.run_id
            FROM test_cases tc
            LEFT JOIN (
                SELECT a.*
                FROM llm_validations a
                INNER JOIN (
                    SELECT case_id, MAX(validated_at) AS max_ts
                    FROM llm_validations
                    GROUP BY case_id
                ) b ON a.case_id = b.case_id AND a.validated_at = b.max_ts
            ) lv ON tc.case_id = lv.case_id
            LEFT JOIN validation_runs vr ON lv.run_id = vr.run_id
            ORDER BY tc.case_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_validation_runs(db_path: Path = DB_DEFAULT_PATH) -> list[dict]:
    """All validation run summaries, newest first."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM validation_runs ORDER BY run_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_case_history(case_id: str, db_path: Path = DB_DEFAULT_PATH) -> list[dict]:
    """All LLM validation attempts for a single case, newest first."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT lv.*, vr.provider, vr.model
            FROM llm_validations lv
            JOIN validation_runs vr ON lv.run_id = vr.run_id
            WHERE lv.case_id = ?
            ORDER BY lv.validated_at DESC
            """,
            (case_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_few_shots(
    case_data: dict,
    db_path: Path = DB_DEFAULT_PATH,
    target: int = 5,
    min_per_level: int = 3,
) -> list[dict]:
    """Retrieve few-shot examples using capability → product → tribe hierarchy.

    Collects up to `target` validated examples. Starts at capability; if fewer
    than `min_per_level` results are found, supplements from product; if still
    fewer than `min_per_level`, supplements from tribe. The case being validated
    is always excluded.

    Returns dicts with keys: case_id, title, capability, product, tribe,
    pre_conditions, test_steps, expected_result, verdict,
    preconditions_passed/remarks, test_steps_passed/remarks,
    expected_results_passed/remarks, overall_remarks, improvements.
    """
    if not db_path.exists():
        return []

    capability = case_data.get("Capability") or case_data.get("capability")
    product    = case_data.get("Product")    or case_data.get("product")
    tribe      = case_data.get("Tribe")      or case_data.get("tribe")
    current_id = case_data.get("Test Case ID") or case_data.get("case_id") or ""

    _LATEST_LLM = """
        JOIN (
            SELECT a.*
            FROM llm_validations a
            INNER JOIN (
                SELECT case_id, MAX(validated_at) AS max_ts
                FROM llm_validations
                GROUP BY case_id
            ) b ON a.case_id = b.case_id AND a.validated_at = b.max_ts
        ) lv ON tc.case_id = lv.case_id
    """

    def _fetch(field: str, value: str, exclude: set, limit: int) -> list[dict]:
        if not value:
            return []
        # Ensure the NOT IN list is never empty (avoids SQL syntax error)
        guard = list(exclude) if exclude else ["__none__"]
        placeholders = ",".join("?" * len(guard))
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    tc.case_id, tc.title, tc.capability, tc.product, tc.tribe,
                    tc.pre_conditions, tc.test_steps, tc.expected_result,
                    lv.verdict,
                    lv.preconditions_passed,    lv.preconditions_remarks,
                    lv.test_steps_passed,       lv.test_steps_remarks,
                    lv.expected_results_passed, lv.expected_results_remarks,
                    lv.overall_remarks, lv.improvements
                FROM test_cases tc
                {_LATEST_LLM}
                WHERE tc.{field} = ?
                  AND lv.verdict IS NOT NULL
                  AND tc.case_id NOT IN ({placeholders})
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (value, *guard, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    examples: list[dict] = []
    seen: set[str] = {current_id}

    # Level 1 — Capability
    if capability:
        found = _fetch("capability", capability, seen, target)
        examples.extend(found)
        seen.update(r["case_id"] for r in found)

    # Level 2 — Product (supplement if too few)
    if len(examples) < min_per_level and product:
        found = _fetch("product", product, seen, target - len(examples))
        examples.extend(found)
        seen.update(r["case_id"] for r in found)

    # Level 3 — Tribe (supplement if still too few)
    if len(examples) < min_per_level and tribe:
        found = _fetch("tribe", tribe, seen, target - len(examples))
        examples.extend(found)
        seen.update(r["case_id"] for r in found)

    return examples[:target]
