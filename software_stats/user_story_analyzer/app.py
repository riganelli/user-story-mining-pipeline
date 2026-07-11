"""User Story Analyzer — Streamlit entry point.

Loads an experiment (or a legacy output folder) and renders the landing
page; the individual analysis pages live under pages/.
"""
from pathlib import Path

import streamlit as st

from core.loader import load_output, list_experiments
from core.ui import inject_css, hero

inject_css()

st.set_page_config(
    page_title="User Story Analyzer",
    page_icon="📊",
    layout="wide",
)

hero(
    "User Story Analyzer",
    "Select an experiment or load an output/ folder to explore the results.",
)

# Sidebar: experiment loading.

with st.sidebar:
    st.header("Loading")

    # The experiments/ folder sits at the project root, next to pipeline/,
    # two levels above this dashboard.
    default_exp_root = st.text_input(
        "experiments/ folder",
        value=str(Path(__file__).resolve().parent.parent.parent / "experiments"),
        help="Path to the folder containing your experiments.",
    )

    experiments = list_experiments(default_exp_root)

    if experiments:
        def _label(e: dict) -> str:
            cfg = e.get("config", {})
            desc = cfg.get("description", "")
            ok = "✅" if e["has_step5"] else "⚠️"
            return f"{ok} {e['id']}" + (f" — {desc[:30]}" if desc else "")

        labels = [_label(e) for e in experiments]
        label_to_id = {lab: e["id"] for lab, e in zip(labels, experiments)}

        sel_label = st.selectbox(
            "Experiment",
            labels,
            help="✅ = full pipeline, ⚠️ = step 5 missing",
        )

        load_exp_btn = st.button("Load experiment", type="primary", use_container_width=True)
    else:
        st.info("No experiment found in this folder.")
        load_exp_btn = False
        sel_label = None
        label_to_id = {}

    st.divider()

    # Manual path fallback for legacy runs or custom folders.
    with st.expander("📂 Load manual path (legacy runs)"):
        path_input = st.text_input(
            "Full path of the output/ folder",
            placeholder=r"C:\path\to\output",
        )
        load_manual_btn = st.button("Load manual path", use_container_width=True)


# Session state.

if "data" not in st.session_state:
    st.session_state.data = None

if load_exp_btn and sel_label:
    exp_id = label_to_id[sel_label]
    target = Path(default_exp_root) / exp_id
    try:
        with st.spinner(f"Loading {exp_id}..."):
            st.session_state.data = load_output(target)
        st.success(f"Experiment {exp_id} loaded.")
    except FileNotFoundError as e:
        st.session_state.data = None
        st.error(str(e))
    except Exception as e:
        st.session_state.data = None
        st.error(f"Unexpected error: {type(e).__name__}: {e}")

if load_manual_btn and path_input:
    try:
        with st.spinner("Loading..."):
            st.session_state.data = load_output(path_input)
        st.success("Output loaded.")
    except FileNotFoundError as e:
        st.session_state.data = None
        st.error(str(e))
    except Exception as e:
        st.session_state.data = None
        st.error(f"Unexpected error: {type(e).__name__}: {e}")


# Landing page.

data = st.session_state.data

if data is None:
    st.info("👈 Select an experiment on the left and press **Load** to start.")
    st.stop()

exp_id = data.experiment_id or data.root.name
st.success(f"✅ Active experiment: `{exp_id}`  ·  path: `{data.root}`")

if data.config:
    with st.expander("📋 Experiment configuration", expanded=False):
        st.json(data.config)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Repos", len(data.repo))
c2.metric("Candidate issues", len(data.issues))
c3.metric("AI validated", len(data.validated_issues))
c4.metric(
    "Validation rate",
    f"{data.validation_stats.get('totals', {}).get('validation_rate_pct', 0):.1f}%",
)

st.divider()

st.subheader("How to navigate")

nav_col1, nav_col2 = st.columns(2)

with nav_col1:
    st.markdown("""
    **🗺️ Overview**  
    Consolidated view of the 5 pipeline steps.
    
    **🔎 Discovery**  
    Analysis of the repository search strategies.
    
    **🤖 AI Validation**  
    Semantic filtering of issues via local LLM.
    """)

with nav_col2:
    st.markdown("""
    **📦 Dataset**  
    Final dataset browser with PRs and diffs.
    
    **🔀 Comparisons**  
    Overlap between discovery strategies.
    
    **💬 Chat**  
    Natural-language Q&A on the run.
    """)