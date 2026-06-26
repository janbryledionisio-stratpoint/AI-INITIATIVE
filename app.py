"""Streamlit UI for the TC Analyzer POC.

Two stages over the same pipeline that powers main.py:

1. Fetch + General Standards filter — generate endpoints, fetch from TestRail,
   evaluate each case (testrail/ + pipeline/). Produces the Pass/Fail report.
2. LLM validation (optional) — run the passed cases through the LLM quality
   evaluator against requirements-web.md (validation/).

Run with:  uv run streamlit run app.py
"""

import datetime
import os
import re
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from testrail import generate_endpoint_file, fetch_test_cases
from pipeline import save_all_cases
from pipeline.loader import get_latest_output_file
from validation import validate_cases, PROVIDER_DEFAULT_MODEL
from validation.validator import PROVIDER_ENV_VAR
from database import get_all_cases, get_validation_runs, get_case_history, DB_DEFAULT_PATH

load_dotenv()

DATA_DIR = Path("data")
OUTPUT_DIR = "output"
VALIDATION_DIR = Path(OUTPUT_DIR) / "llm_validation"
TESTRAIL_BASE = "https://myntfintech.testrail.io/index.php?/cases/view/"

# The 6 active General Standards criteria — mirrors evaluate_case() in
# pipeline/filter.py (Automation Status passes on "Not Started" or "Reviewed = Passed DOR").
CRITERIA = [
    ("Automatability", "Automatable, To be Determined", "exact"),
    ("Testing Type", "Web", "exact"),
    ("Automation Status", "Not Started", "exact"),
    ("Status", "New, Reviewed, Active", "exact"),
    ("Priority/Risk", "must not be null", "present"),
    ("Testing Phase", "BE Regression, FE Regression, Regression", "any of"),
]

# Selectable models per provider for the LLM validation tab. The first entry is
# the provider default (mirrors PROVIDER_DEFAULT_MODEL in validation/validator.py);
# a "Custom…" option in the UI lets you type any model ID not listed here.
PROVIDER_MODELS = {
    "anthropic": [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o1-mini"],
    "google": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
    "ollama": ["llama3.3", "llama3.2", "qwen2.5", "mistral"],
    "claude-code": ["claude-code"],
}


st.set_page_config(
    page_title="TC Analyzer",
    page_icon="🤖",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_validation_df(df: pd.DataFrame) -> pd.DataFrame:
    """Upgrade old validation CSVs (Overall Passed / Summary columns) to the current schema."""
    df = df.copy()
    if "Verdict" not in df.columns and "Overall Passed" in df.columns:
        df["Verdict"] = df["Overall Passed"].map(
            {True: "Passed", 1: "Passed", False: "Failed", 0: "Failed"}
        )
    if "Overall Remarks" not in df.columns:
        df["Overall Remarks"] = df.get("Summary", "")
    if "Improvements" not in df.columns:
        df["Improvements"] = ""
    return df


def categorize_remark(remark: str) -> str:
    """Map a single failure remark to its criterion name for aggregation."""
    remark = remark.strip()
    if " = " in remark:
        return remark.split(" = ", 1)[0].strip()
    if remark.lower().startswith("priority"):
        return "Priority/Risk"
    return remark


def failure_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """Count how often each criterion is the reason a case failed."""
    counts: dict[str, int] = {}
    failed = df[df["General Standards"] == "Failed"]
    for remarks in failed["Remarks"].fillna(""):
        for part in str(remarks).split(";"):
            part = part.strip()
            if not part:
                continue
            name = categorize_remark(part)
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return pd.DataFrame(columns=["Criterion", "Failed cases"])
    return (
        pd.DataFrame(
            {"Criterion": list(counts.keys()),
             "Failed cases": list(counts.values())}
        )
        .sort_values("Failed cases", ascending=False)
        .reset_index(drop=True)
    )


def _passed_badge(value) -> str:
    """Render a True/False/None pass flag as an emoji badge.

    Tolerates Python bool, numpy.bool_, and NaN (errored rows) — pandas yields
    different types depending on whether the column has any null values.
    """
    if pd.isna(value):
        return "⚠️ Error"
    return "✅ Pass" if bool(value) else "❌ Fail"


def render_validation_card(row: pd.Series) -> None:
    """One expandable card per validated case — criteria side-by-side, no
    horizontal scrolling."""
    verdict = row.get("Verdict")
    icon = {"Passed": "✅", "Needs Improvement": "🔧", "Failed": "❌"}.get(verdict, "⚠️")
    title = row.get("Title") or ""
    with st.expander(f"{icon}  {row.get('Test Case ID', '')} — {title}"):
        criteria = [
            ("Pre Conditions", "Preconditions Passed", "Preconditions Remarks"),
            ("Test Steps", "Test Steps Passed", "Test Steps Remarks"),
            ("Expected Results", "Expected Results Passed", "Expected Results Remarks"),
        ]
        for col, (label, pass_key, rem_key) in zip(st.columns(3), criteria):
            col.markdown(f"**{label}**")
            col.markdown(_passed_badge(row.get(pass_key)))
            remark = row.get(rem_key)
            if isinstance(remark, str) and remark.strip():
                col.caption(remark)

        overall_remarks = row.get("Overall Remarks")
        if isinstance(overall_remarks, str) and overall_remarks.strip():
            st.markdown(f"**Overall Remarks** — {overall_remarks}")

        improvements = row.get("Improvements")
        if isinstance(improvements, str) and improvements.strip():
            st.info(f"**Improvements** — {improvements}")


def latest_validation_file() -> Path | None:
    """Most recent saved LLM validation report, or None if there are none."""
    if not VALIDATION_DIR.exists():
        return None
    files = sorted(VALIDATION_DIR.glob("llm_validation_*.csv"))
    return files[-1] if files else None


def run_pipeline(source_csv: Path, credentials) -> pd.DataFrame:
    """Run fetch + filter, persist the report, and return it as a DataFrame.

    The colleague's fetch_test_cases() reads credentials from the environment,
    so a manual override is applied to os.environ for the duration of the run.
    """
    if credentials is not None:
        os.environ["TESTRAIL_USERNAME"], os.environ["TESTRAIL_PASSWORD"] = credentials

    status = st.status("Running TC Analyzer pipeline…", expanded=True)

    with status:
        st.write(f"**Stage 1/3 — Generating endpoints** from `{source_csv.name}`")
        endpoint_file = generate_endpoint_file(str(source_csv))
        endpoint_count = len(pd.read_csv(endpoint_file))
        st.write(f"Built {endpoint_count} TestRail API endpoint(s).")

        st.write("**Stage 2/3 — Fetching cases from TestRail** (concurrent)")
        with st.spinner(f"Fetching {endpoint_count} case(s)…"):
            results = fetch_test_cases(endpoint_file)
        ok_count = sum(1 for r in results if r["status_code"] == 200)
        st.write(f"Fetched {ok_count}/{endpoint_count} case(s) successfully.")

        st.write("**Stage 3/3 — Evaluating against General Standards**")
        output_file = save_all_cases(results, output_dir=OUTPUT_DIR)
        if output_file is None:
            df = pd.DataFrame()
        else:
            df = pd.read_csv(output_file)
        st.write(f"Evaluated {len(df)} case(s) → `{output_file}`")

    if df.empty:
        status.update(label="Pipeline finished — no cases returned.", state="error")
    else:
        status.update(label="Pipeline complete ✓", state="complete", expanded=False)
    return df


# ---------------------------------------------------------------------------
# Sidebar — configuration & run
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Configuration")

    st.subheader("TestRail credentials")
    env_user = os.getenv("TESTRAIL_USERNAME")
    env_key = os.getenv("TESTRAIL_PASSWORD")
    has_env = bool(env_user and env_key)

    use_env = st.checkbox(
        "Use credentials from .env",
        value=has_env,
        help="Reads TESTRAIL_USERNAME / TESTRAIL_PASSWORD (API key) from .env.",
    )

    credentials = None
    if use_env:
        if has_env:
            st.caption(f"Using `{env_user}` (API key loaded, hidden).")
        else:
            st.warning("No credentials found in .env.")
    else:
        manual_user = st.text_input("Username (email)", value=env_user or "")
        manual_key = st.text_input(
            "API key", type="password",
            help="TestRail API key (My Settings → API Keys), not the password.",
        )
        if manual_user and manual_key:
            credentials = (manual_user, manual_key)

    st.divider()

    st.subheader("Data source")
    mode = st.radio(
        "TestRail export to analyze",
        ["Upload CSV", "Existing file in data/"],
        label_visibility="collapsed",
    )

    source_csv: Path | None = None
    if mode == "Upload CSV":
        uploaded = st.file_uploader(
            "Raw TestRail export (needs ID + Testrail Link columns)",
            type="csv",
        )
        if uploaded is not None:
            tmp_dir = Path(tempfile.gettempdir())
            source_csv = tmp_dir / uploaded.name
            source_csv.write_bytes(uploaded.getvalue())
    else:
        existing = sorted(p.name for p in DATA_DIR.glob("*.csv")) \
            if DATA_DIR.exists() else []
        if existing:
            choice = st.selectbox("File", existing)
            source_csv = DATA_DIR / choice
        else:
            st.info("No CSV files found in data/.")

    st.divider()

    creds_ready = use_env and has_env or credentials is not None
    run_clicked = st.button(
        "▶ Run analysis",
        type="primary",
        width="stretch",
        disabled=source_csv is None or not creds_ready,
    )
    if source_csv is None:
        st.caption("Select or upload a CSV to enable the run.")
    elif not creds_ready:
        st.caption("Provide TestRail credentials to enable the run.")


# ---------------------------------------------------------------------------
# Main — header & criteria reference
# ---------------------------------------------------------------------------
st.title("🤖 TC Analyzer")
st.caption(
    "Pulls test cases from TestRail and flags which ones meet the automation "
    "**General Standards**. POC for the API Agentic AI initiative."
)

with st.expander("General Standards criteria (a case passes only if ALL hold)"):
    st.table(
        pd.DataFrame(
            CRITERIA, columns=["Criterion", "Allowed values", "Match rule"]
        )
    )

# ---------------------------------------------------------------------------
# Link Builder — enrich an ID-only CSV with the Testrail Link column
# ---------------------------------------------------------------------------
with st.expander("🔗 Link Builder — Add Testrail Link column to an ID-only CSV"):
    st.caption(
        "Upload a CSV that has an **ID** column (e.g. `C2615919`) but is missing "
        "the `Testrail Link` column. The tool strips the `C` prefix and builds the "
        "full URL so the file is ready to drop into the pipeline above."
    )
    lb_upload = st.file_uploader("Upload CSV (ID + Title)", type="csv", key="lb_upload")

    if lb_upload is not None:
        lb_df = pd.read_csv(lb_upload)

        if "ID" not in lb_df.columns:
            st.error("CSV must have an **ID** column (e.g. `C2615919`).")
        elif "Testrail Link" in lb_df.columns:
            st.info("This CSV already has a `Testrail Link` column — nothing to add.")
            st.dataframe(lb_df, hide_index=True, width="stretch")
        else:
            def _build_link(raw_id: str) -> str:
                numeric = re.sub(r"[^\d]", "", str(raw_id))
                return f"{TESTRAIL_BASE}{numeric}" if numeric else ""

            lb_df.insert(
                lb_df.columns.get_loc("ID") + 1,
                "Testrail Link",
                lb_df["ID"].apply(_build_link),
            )

            st.success(f"Generated `Testrail Link` for {len(lb_df)} row(s).")
            st.dataframe(lb_df, hide_index=True, width="stretch")

            enriched_name = lb_upload.name.replace(".csv", "_with_links.csv")
            st.download_button(
                "⬇ Download enriched CSV",
                data=lb_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=enriched_name,
                mime="text/csv",
            )

            if st.button("💾 Save to data/ (makes it available in the pipeline sidebar)"):
                DATA_DIR.mkdir(exist_ok=True)
                save_path = DATA_DIR / enriched_name
                lb_df.to_csv(save_path, index=False, encoding="utf-8-sig")
                st.success(f"Saved to `{save_path}` — select it from the sidebar to run the pipeline.")

st.divider()

# Run on click, otherwise fall back to the latest report already on disk.
if run_clicked and source_csv is not None:
    try:
        st.session_state["df"] = run_pipeline(source_csv, credentials)
        st.session_state["source_name"] = source_csv.name
        st.session_state.pop("validation_df", None)  # stale once data changes
    except Exception as exc:  # surface pipeline errors in the UI
        st.error(f"Pipeline failed: {type(exc).__name__}: {exc}")

if "df" not in st.session_state:
    try:
        latest = get_latest_output_file(OUTPUT_DIR)
        st.session_state["df"] = pd.read_csv(latest)
        st.session_state["source_name"] = f"{latest.name} (latest on disk)"
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Main — results
# ---------------------------------------------------------------------------
df = st.session_state.get("df")

if df is None:
    st.info("Configure a data source and credentials in the sidebar, then "
            "**Run analysis** to generate the Pass/Fail report.")
    st.stop()

if df.empty:
    st.warning("The last run returned no evaluable cases.")
    st.stop()

st.success(f"Showing results for **{st.session_state.get('source_name', '')}**")

total = len(df)
passed = int((df["General Standards"] == "Passed").sum())
failed = total - passed
rate = passed / total if total else 0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total cases", total)
c2.metric("Passed", passed)
c3.metric("Failed", failed)
c4.metric("Pass rate", f"{rate:.0%}")

tab_results, tab_detail, tab_insights, tab_validate, tab_db = st.tabs(
    ["📋 Results", "🔍 Case detail", "📊 Insights", "🧪 LLM Validation", "🗄️ Database"]
)

with tab_results:
    fcol, scol = st.columns([1, 2])
    status_filter = fcol.selectbox("Status", ["All", "Passed", "Failed"])
    search = scol.text_input(
        "Search title or ID", placeholder="e.g. Jarvis or C33699"
    )

    view = df
    if status_filter != "All":
        view = view[view["General Standards"] == status_filter]
    if search:
        s = search.lower()
        view = view[
            view["Title"].fillna("").str.lower().str.contains(s)
            | view["Test Case ID"].fillna("").str.lower().str.contains(s)
        ]

    st.caption(f"{len(view)} of {total} case(s)")

    # Compact overview — key columns only, so the table fits without horizontal
    # scroll. Full fields (Remarks, Pre Conditions, Steps, …) live in Case detail;
    # the download still exports every column.
    overview_cols = [
        "General Standards", "Test Case ID", "Title",
        "Priority", "Automation Status", "Status",
    ]
    display = view[[c for c in overview_cols if c in view.columns]]
    st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        column_config={
            "General Standards": st.column_config.TextColumn("Result", width="small"),
            "Title": st.column_config.TextColumn(width="large"),
        },
    )
    st.caption("Open the 🔍 Case detail tab for remarks, preconditions, and steps.")

    st.download_button(
        "⬇ Download filtered CSV (all columns)",
        data=view.to_csv(index=False).encode("utf-8-sig"),
        file_name="testcases_with_standards.csv",
        mime="text/csv",
    )

with tab_detail:
    ids = df["Test Case ID"].tolist()
    selected = st.selectbox("Test case", ids)
    row = df[df["Test Case ID"] == selected].iloc[0]

    result = row["General Standards"]
    if result == "Passed":
        st.success(f"**{selected}** — Passed ✓")
    else:
        st.error(f"**{selected}** — Failed")
        st.write("**Remarks:**", row.get("Remarks") or "—")

    st.subheader(row.get("Title") or "(no title)")

    meta = {
        "Priority": row.get("Priority"),
        "Automatability": row.get("Automatability"),
        "Automation Status": row.get("Automation Status"),
        "Testing Type": row.get("Testing Type"),
        "Status": row.get("Status"),
        "Testing Phase": row.get("Testing Phase"),
        "Project Initiative": row.get("Project Initiative"),
    }
    mcols = st.columns(3)
    for i, (k, v) in enumerate(meta.items()):
        mcols[i % 3].metric(k, str(v) if pd.notna(v) and v != "" else "—")

    st.markdown("**Pre Conditions**")
    st.text(row.get("Pre Conditions") or "—")
    st.markdown("**Test Steps**")
    st.text(row.get("Test Steps") or "—")
    st.markdown("**Expected Result**")
    st.text(row.get("Expected Result") or "—")

with tab_insights:
    left, right = st.columns(2)

    with left:
        st.markdown("**Pass vs. Fail**")
        st.bar_chart(df["General Standards"].value_counts(), color="#4c78a8")

    with right:
        st.markdown("**Why cases fail (by criterion)**")
        breakdown = failure_breakdown(df)
        if breakdown.empty:
            st.caption("No failures 🎉")
        else:
            st.bar_chart(
                breakdown.set_index("Criterion")["Failed cases"],
                color="#e45756",
                horizontal=True,
            )
            st.dataframe(breakdown, hide_index=True, width="stretch")

with tab_validate:
    st.markdown(
        "Run the **passed** cases through the LLM quality evaluator "
        "(`requirements-web.md` §3.2–3.4). This calls an external LLM — it "
        "takes time and may incur cost."
    )

    passed_cases = df[df["General Standards"] == "Passed"].to_dict("records")
    st.caption(f"{len(passed_cases)} passed case(s) eligible for validation.")

    pcol, mcol, dcol, wcol = st.columns([1, 1, 1, 1])
    providers = list(PROVIDER_DEFAULT_MODEL)
    provider = pcol.selectbox("Provider", providers, index=providers.index("anthropic"))

    CUSTOM_MODEL = "Custom…"
    model_options = PROVIDER_MODELS.get(
        provider, [PROVIDER_DEFAULT_MODEL[provider]]
    ) + [CUSTOM_MODEL]
    model_choice = mcol.selectbox(
        "Model", model_options,
        help=f"Provider default: {PROVIDER_DEFAULT_MODEL[provider]}",
    )
    delay = dcol.number_input(
        "Delay (s) between calls", min_value=0.0, value=0.0, step=0.5,
        help="Use 4+ for Google free tier. Ignored when Workers > 1.",
    )
    max_workers = wcol.number_input(
        "Workers (parallel)", min_value=1, max_value=20, value=1, step=1,
        help="Number of cases validated simultaneously. 1 = sequential. "
             "Increase for paid API tiers; keep at 1 for free-tier rate limits.",
    )

    if model_choice == CUSTOM_MODEL:
        model = st.text_input(
            "Custom model ID", placeholder=PROVIDER_DEFAULT_MODEL[provider],
            help="Leave blank to use the provider default.",
        ).strip()
    else:
        model = model_choice

    env_var = PROVIDER_ENV_VAR.get(provider)
    key_ok = env_var is None or bool(os.getenv(env_var))
    if not key_ok:
        st.warning(f"`{env_var}` is not set — required for the '{provider}' provider.")

    run_col, load_col = st.columns([1, 1])
    validate_clicked = run_col.button(
        "🧪 Validate passed cases",
        type="primary",
        disabled=not passed_cases or not key_ok,
    )
    load_clicked = load_col.button(
        "📂 Load latest saved report",
        help="View the most recent report in output/llm_validation/ — no LLM calls, no token usage.",
    )

    if validate_clicked:
        try:
            with st.spinner(f"Validating {len(passed_cases)} case(s) with {provider}…"):
                out = validate_cases(
                    passed_cases,
                    output_dir=OUTPUT_DIR,
                    provider=provider,
                    model=model or None,
                    delay=delay,
                    max_workers=int(max_workers),
                )
            st.session_state["validation_df"] = _normalize_validation_df(pd.read_csv(out))
            st.session_state["validation_file"] = str(out)
        except Exception as exc:
            st.error(f"Validation failed: {type(exc).__name__}: {exc}")

    if load_clicked:
        latest = latest_validation_file()
        if latest is None:
            st.info("No saved validation reports found in output/llm_validation/.")
        else:
            st.session_state["validation_df"] = _normalize_validation_df(pd.read_csv(latest))
            st.session_state["validation_file"] = str(latest)

    # Auto-load the latest saved report on first visit so the UI isn't empty.
    if "validation_df" not in st.session_state:
        latest = latest_validation_file()
        if latest is not None:
            st.session_state["validation_df"] = _normalize_validation_df(pd.read_csv(latest))
            st.session_state["validation_file"] = f"{latest} (loaded from disk)"

    vdf = st.session_state.get("validation_df")
    if vdf is not None:
        st.success(f"Validation report: `{st.session_state.get('validation_file', '')}`")

        v_passed = int((vdf["Verdict"] == "Passed").sum())
        v_needs_improvement = int((vdf["Verdict"] == "Needs Improvement").sum())
        v_failed = int((vdf["Verdict"] == "Failed").sum())
        v_errors = int(vdf["Verdict"].isna().sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Passed", v_passed)
        m2.metric("Needs Improvement", v_needs_improvement)
        m3.metric("Failed", v_failed)
        m4.metric("Errors", v_errors)

        vfilter = st.radio(
            "Show", ["All", "Passed", "Needs Improvement", "Failed", "Errors"],
            horizontal=True, label_visibility="collapsed",
        )
        if vfilter == "Passed":
            vview = vdf[vdf["Verdict"] == "Passed"]
        elif vfilter == "Needs Improvement":
            vview = vdf[vdf["Verdict"] == "Needs Improvement"]
        elif vfilter == "Failed":
            vview = vdf[vdf["Verdict"] == "Failed"]
        elif vfilter == "Errors":
            vview = vdf[vdf["Verdict"].isna()]
        else:
            vview = vdf

        st.caption(f"{len(vview)} of {len(vdf)} case(s)")
        for _, row in vview.iterrows():
            render_validation_card(row)

        with st.expander("Raw table & download"):
            st.dataframe(vdf, width="stretch", hide_index=True)
            st.download_button(
                "⬇ Download validation CSV",
                data=vdf.to_csv(index=False).encode("utf-8-sig"),
                file_name="llm_validation.csv",
                mime="text/csv",
            )

with tab_db:
    st.markdown(
        "Persisted results from every LLM validation run. "
        f"Database: `{DB_DEFAULT_PATH}`"
    )

    db_cases = get_all_cases(DB_DEFAULT_PATH)
    db_runs  = get_validation_runs(DB_DEFAULT_PATH)

    if not db_cases:
        st.info("No data yet — run an LLM validation to populate the database.")
        st.stop()

    db_df = pd.DataFrame(db_cases)

    # ── Summary metrics ──────────────────────────────────────────────────────
    total_db   = len(db_df)
    llm_pass   = int((db_df["verdict"] == "Passed").sum())
    llm_ni     = int((db_df["verdict"] == "Needs Improvement").sum())
    llm_fail   = int((db_df["verdict"] == "Failed").sum())
    llm_error  = int(db_df["verdict"].isna().sum())

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total cases", total_db)
    m2.metric("Passed", llm_pass)
    m3.metric("Needs Improvement", llm_ni)
    m4.metric("Failed", llm_fail)
    m5.metric("Errors", llm_error)
    m6.metric("Runs", len(db_runs))

    st.divider()

    # ── Filters ──────────────────────────────────────────────────────────────
    st.markdown("**Filters** — all filters combine; any field left at 'All' / blank is ignored.")

    # Row 1: categorical filters
    fc1, fc2, fc3, fc4 = st.columns(4)

    tribe_opts   = ["All"] + sorted(db_df["tribe"].dropna().unique().tolist())
    product_opts = ["All"] + sorted(db_df["product"].dropna().unique().tolist())
    cap_opts     = ["All"] + sorted(db_df["capability"].dropna().unique().tolist())

    tribe_filter   = fc1.selectbox("Tribe",      tribe_opts,   key="db_tribe")
    product_filter = fc2.selectbox("Product",    product_opts, key="db_product")
    cap_filter     = fc3.selectbox("Capability", cap_opts,     key="db_capability")
    result_filter  = fc4.selectbox(
        "LLM Result",
        ["All", "Passed", "Needs Improvement", "Failed", "Error"],
        key="db_result",
    )

    # Row 2: date range + search
    dc1, dc2, dc3, dc4 = st.columns([1, 1, 1, 2])

    date_field = dc1.selectbox(
        "Date field",
        ["Validated At", "First Seen", "Last Updated"],
        key="db_date_field",
    )
    _date_col_map = {
        "Validated At": "validated_at",
        "First Seen":   "first_seen_at",
        "Last Updated": "last_updated_at",
    }
    _date_col = _date_col_map[date_field]

    _date_series = pd.to_datetime(db_df[_date_col], errors="coerce", utc=True).dt.date
    _valid_dates = _date_series.dropna()
    _min_date = (
        _valid_dates.min() if not _valid_dates.empty
        else datetime.date.today() - datetime.timedelta(days=365)
    )
    _max_date = _valid_dates.max() if not _valid_dates.empty else datetime.date.today()

    start_date = dc2.date_input("Start date", value=_min_date, key="db_start_date")
    end_date   = dc3.date_input("End date",   value=_max_date, key="db_end_date")
    db_search  = dc4.text_input("Search title or ID", key="db_search")

    # Clear-all button
    if st.button("🗑 Clear all filters", key="db_clear_filters"):
        for _k in [
            "db_tribe", "db_product", "db_capability", "db_result",
            "db_date_field", "db_start_date", "db_end_date", "db_search",
        ]:
            st.session_state.pop(_k, None)
        st.rerun()

    # Apply all filters (fully combinable)
    view_db = db_df.copy()
    view_db["_filter_date"] = pd.to_datetime(
        view_db[_date_col], errors="coerce", utc=True
    ).dt.date

    if result_filter in ("Passed", "Needs Improvement", "Failed"):
        view_db = view_db[view_db["verdict"] == result_filter]
    elif result_filter == "Error":
        view_db = view_db[view_db["verdict"].isna()]
    if tribe_filter != "All":
        view_db = view_db[view_db["tribe"] == tribe_filter]
    if product_filter != "All":
        view_db = view_db[view_db["product"] == product_filter]
    if cap_filter != "All":
        view_db = view_db[view_db["capability"] == cap_filter]
    if start_date:
        view_db = view_db[view_db["_filter_date"] >= start_date]
    if end_date:
        view_db = view_db[view_db["_filter_date"] <= end_date]
    if db_search:
        s = db_search.lower()
        view_db = view_db[
            view_db["title"].fillna("").str.lower().str.contains(s)
            | view_db["case_id"].fillna("").str.lower().str.contains(s)
        ]

    view_db = view_db.drop(columns=["_filter_date"], errors="ignore")

    def _llm_badge(val):
        badges = {
            "Passed":           "✅ Passed",
            "Needs Improvement":"🔧 Needs Improvement",
            "Failed":           "❌ Failed",
        }
        return badges.get(val, "⚠️ Error") if isinstance(val, str) else "⚠️ Error"

    display_db = view_db[[
        "case_id", "title", "tribe", "product", "capability",
        "verdict", "preconditions_passed", "test_steps_passed",
        "expected_results_passed", "overall_remarks", "improvements",
        "provider", "model", "validated_at",
    ]].copy()
    display_db["verdict"] = display_db["verdict"].apply(_llm_badge)

    st.caption(f"{len(view_db)} of {total_db} case(s)")
    st.dataframe(
        display_db,
        hide_index=True,
        width="stretch",
        column_config={
            "case_id":        st.column_config.TextColumn("ID", width="small"),
            "title":          st.column_config.TextColumn("Title", width="large"),
            "verdict":        st.column_config.TextColumn("LLM Result", width="small"),
            "overall_remarks":st.column_config.TextColumn("Overall Remarks", width="large"),
            "improvements":   st.column_config.TextColumn("Improvements", width="large"),
        },
    )

    st.download_button(
        "⬇ Download filtered CSV",
        data=view_db.to_csv(index=False).encode("utf-8-sig"),
        file_name="tc_analyzer_db_export.csv",
        mime="text/csv",
        key="db_download",
    )

    # ── Per-case history ─────────────────────────────────────────────────────
    st.divider()
    st.subheader("Case validation history")
    all_ids = db_df["case_id"].tolist()
    selected_id = st.selectbox("Select a case", all_ids, key="db_case_select")
    if selected_id:
        history = get_case_history(selected_id, DB_DEFAULT_PATH)
        if history:
            st.caption(f"{len(history)} validation run(s) for **{selected_id}**")
            for h in history:
                verdict = h.get("verdict")
                icon = {"Passed": "✅", "Needs Improvement": "🔧", "Failed": "❌"}.get(verdict, "⚠️")
                with st.expander(
                    f"{icon} Run {h['run_id']} — {h['provider']} / {h['model']}"
                ):
                    c1, c2, c3 = st.columns(3)
                    c1.markdown("**Pre Conditions**")
                    c1.markdown("✅" if h["preconditions_passed"] == 1 else "❌" if h["preconditions_passed"] == 0 else "⚠️")
                    c1.caption(h.get("preconditions_remarks") or "—")
                    c2.markdown("**Test Steps**")
                    c2.markdown("✅" if h["test_steps_passed"] == 1 else "❌" if h["test_steps_passed"] == 0 else "⚠️")
                    c2.caption(h.get("test_steps_remarks") or "—")
                    c3.markdown("**Expected Results**")
                    c3.markdown("✅" if h["expected_results_passed"] == 1 else "❌" if h["expected_results_passed"] == 0 else "⚠️")
                    c3.caption(h.get("expected_results_remarks") or "—")
                    if h.get("overall_remarks"):
                        st.markdown(f"**Overall Remarks** — {h['overall_remarks']}")
                    if h.get("improvements"):
                        st.info(f"**Improvements** — {h['improvements']}")
        else:
            st.info("No validation history found for this case.")

    # ── Run history table ────────────────────────────────────────────────────
    st.divider()
    st.subheader("Validation run history")
    if db_runs:
        st.dataframe(
            pd.DataFrame(db_runs),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No runs recorded yet.")
