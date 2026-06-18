"""Streamlit UI for the TC Analyzer POC.

Two stages over the same pipeline that powers main.py:

1. Fetch + General Standards filter — generate endpoints, fetch from TestRail,
   evaluate each case (testrail/ + pipeline/). Produces the Pass/Fail report.
2. LLM validation (optional) — run the passed cases through the LLM quality
   evaluator against requirements-web.md (validation/).

Run with:  uv run streamlit run app.py
"""

import os
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

load_dotenv()

DATA_DIR = Path("data")
OUTPUT_DIR = "output"
VALIDATION_DIR = Path(OUTPUT_DIR) / "llm_validation"

# The 6 active General Standards criteria — mirrors evaluate_case() in
# pipeline/filter.py (Automation Status passes on "Not Started" or "Reviewed = Passed DOR").
CRITERIA = [
    ("Automatability", "Automatable, To be Determined", "exact"),
    ("Testing Type", "Web", "exact"),
    ("Automation Status", "Not Started, Reviewed = Passed DOR", "exact"),
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
}


st.set_page_config(
    page_title="TC Analyzer",
    page_icon="🤖",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
    overall = row.get("Overall Passed")
    icon = {True: "✅", False: "❌"}.get(overall, "⚠️")
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

        summary = row.get("Summary")
        if isinstance(summary, str) and summary.strip():
            st.markdown(f"**Summary** — {summary}")


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

tab_results, tab_detail, tab_insights, tab_validate = st.tabs(
    ["📋 Results", "🔍 Case detail", "📊 Insights", "🧪 LLM Validation"]
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

    pcol, mcol, dcol = st.columns([1, 1, 1])
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
        help="Use 4+ for Google free tier.",
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
                )
            st.session_state["validation_df"] = pd.read_csv(out)
            st.session_state["validation_file"] = str(out)
        except Exception as exc:
            st.error(f"Validation failed: {type(exc).__name__}: {exc}")

    if load_clicked:
        latest = latest_validation_file()
        if latest is None:
            st.info("No saved validation reports found in output/llm_validation/.")
        else:
            st.session_state["validation_df"] = pd.read_csv(latest)
            st.session_state["validation_file"] = str(latest)

    # Auto-load the latest saved report on first visit so the UI isn't empty.
    if "validation_df" not in st.session_state:
        latest = latest_validation_file()
        if latest is not None:
            st.session_state["validation_df"] = pd.read_csv(latest)
            st.session_state["validation_file"] = f"{latest} (loaded from disk)"

    vdf = st.session_state.get("validation_df")
    if vdf is not None:
        st.success(f"Validation report: `{st.session_state.get('validation_file', '')}`")

        v_passed = int(vdf["Overall Passed"].eq(True).sum())
        v_failed = int(vdf["Overall Passed"].eq(False).sum())
        v_errors = int(vdf["Overall Passed"].isna().sum())
        m1, m2, m3 = st.columns(3)
        m1.metric("Passed", v_passed)
        m2.metric("Failed", v_failed)
        m3.metric("Errors", v_errors)

        vfilter = st.radio(
            "Show", ["All", "Passed", "Failed", "Errors"],
            horizontal=True, label_visibility="collapsed",
        )
        if vfilter == "Passed":
            vview = vdf[vdf["Overall Passed"].eq(True)]
        elif vfilter == "Failed":
            vview = vdf[vdf["Overall Passed"].eq(False)]
        elif vfilter == "Errors":
            vview = vdf[vdf["Overall Passed"].isna()]
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
