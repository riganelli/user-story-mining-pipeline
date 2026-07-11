"""
Overview page — consolidated view of the 5 pipeline steps.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.ui import inject_css, page_hero, PLOTLY_SEQUENCE, PLOTLY_COLORS, plotly_layout
inject_css()

st.set_page_config(page_title="Overview", page_icon="🗺️", layout="wide")

if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data

page_hero("🗺️ Pipeline Overview", "Consolidated view of the 5 steps of the user story discovery pipeline.")

# Global numbers.
disc_stats = data.discovery_stats
vstats = data.validation_stats
ck_stats = data.dataset_checkpoint.get("stats", {})

n_repo = len(data.repo)
n_issues = len(data.issues)
n_with_pr = len(data.issues_with_pr)
n_without_pr = len(data.issues_without_pr)
n_validate = len(data.validated_issues)
n_discarded = len(data.discarded_issues)
n_dataset_input = ck_stats.get("total_input", 0)
n_dataset_done = len(data.dataset_checkpoint.get("processed", []))
n_dataset_fallite = ck_stats.get("failed", 0)

t_discovery = disc_stats.get("total_time_seconds", 0)
t_issue_extraction = data.issue_extraction_stats.get("total_time_seconds", 0)
t_pr_linking = data.pr_linking_stats.get("total_time_seconds", 0)
t_validation = vstats.get("total_time_seconds", 0)
t_dataset = data.dataset_stats.get("total_time_seconds", 0)

# KPI header.
c1, c2, c3, c4 = st.columns(4)
c1.metric("Discovered repos", n_repo)
c2.metric("Candidate issues", n_issues)
c3.metric("Validated issues", n_validate)
c4.metric("Issues in dataset", n_dataset_done - n_dataset_fallite)

st.divider()

# Step 1: repo discovery (an expansion, not a filter).
st.subheader("Step 1 · Repository discovery")
st.caption(
    "GitHub analysis through 5 search strategies (LABEL, TEMPLATE, TAG, "
    "MENTION, CONNEXTRA) to locate repositories that contain user stories."
)

s1_col1, s1_col2, s1_col3 = st.columns(3)
s1_col1.metric("Discovered repositories", n_repo)
s1_col2.metric("Strategies used", len(disc_stats.get("per_strategy", {})) or 5)
s1_col3.metric("Time", f"{t_discovery:.0f}s", f"≈ {t_discovery/60:.1f} min")

st.divider()

# Funnel from step 2 to 5: progressive issue filtering.
st.subheader("Issue filtering funnel (Step 2 → Step 5)")
st.caption(
    f"From the {n_repo} discovered repositories, candidate issues are extracted "
    "and then progressively filtered. Each percentage shows how much is kept "
    "relative to the previous step."
)

stages = [
    ("Step 2 · Candidate issues", n_issues),
    ("Step 3 · With linked PR", n_with_pr),
    ("Step 4 · AI validated", n_validate),
    ("Step 5 · Final dataset", n_dataset_done - n_dataset_fallite),
]

fig_funnel = go.Figure(go.Funnel(
    y=[s[0] for s in stages],
    x=[s[1] for s in stages],
    textposition="inside",
    textinfo="value+percent previous",
    marker={"color": PLOTLY_SEQUENCE[:4]},
    connector={"line": {"color": "rgba(255,255,255,0.1)", "width": 1}},
))
fig_funnel.update_layout(**plotly_layout(height=380))
st.plotly_chart(fig_funnel, use_container_width=True)

# Per-step detail table.
st.subheader("Per-step detail")

total_discarded_discovery = (
    disc_stats.get('global_totals', {}).get('discarded_regex', 0) +
    disc_stats.get('global_totals', {}).get('discarded_stars', 0) +
    disc_stats.get('global_totals', {}).get('discarded_language', 0)
)
total_discarded_pipeline = total_discarded_discovery + n_without_pr + n_discarded + n_dataset_fallite

df_steps = pd.DataFrame([
    {
        "Step": "1. Discovery",
        "Input": "—",
        "Output": f"{n_repo} repos",
        "Rejected": f"{total_discarded_discovery} "
                    f"(regex: {disc_stats.get('global_totals', {}).get('discarded_regex', 0)}, "
                    f"stars: {disc_stats.get('global_totals', {}).get('discarded_stars', 0)}, "
                    f"language: {disc_stats.get('global_totals', {}).get('discarded_language', 0)})",
        "Time": f"{t_discovery:.0f}s",
    },
    {
        "Step": "2. Issue extraction",
        "Input": f"{n_repo} repos",
        "Output": f"{n_issues} issues",
        "Rejected": "—",
        "Time": f"{t_issue_extraction:.0f}s" if t_issue_extraction else "—",
    },
    {
        "Step": "3. PR linking",
        "Input": f"{n_issues} issues",
        "Output": f"{n_with_pr} with PR",
        "Rejected": f"{n_without_pr} without PR",
        "Time": f"{t_pr_linking:.0f}s" if t_pr_linking else "—",
    },
    {
        "Step": "4. AI validation",
        "Input": f"{n_with_pr} issues",
        "Output": f"{n_validate} validated",
        "Rejected": f"{n_discarded} not user stories",
        "Time": f"{t_validation:.0f}s",
    },
    {
        "Step": "5. Build dataset",
        "Input": f"{n_dataset_input} issues",
        "Output": f"{n_dataset_done - n_dataset_fallite} in dataset",
        "Rejected": f"{n_dataset_fallite} failed",
        "Time": f"{t_dataset:.0f}s" if t_dataset else "—",
    },
    {
        "Step": "**TOTAL**",
        "Input": "—",
        "Output": f"**{n_dataset_done - n_dataset_fallite} user stories**",
        "Rejected": f"**{total_discarded_pipeline}**",
        "Time": f"**{t_discovery + t_issue_extraction + t_pr_linking + t_validation + t_dataset:.0f}s**",
    },
])
st.dataframe(df_steps, use_container_width=True, hide_index=True)

st.divider()

# Times.
st.subheader("Recorded times")

col_t1, col_t2 = st.columns(2)

with col_t1:
    df_times = pd.DataFrame([
        {"step": "1. Discovery", "time_s": t_discovery},
        {"step": "2. Issue extraction", "time_s": t_issue_extraction},
        {"step": "3. PR linking", "time_s": t_pr_linking},
        {"step": "4. AI validation", "time_s": t_validation},
        {"step": "5. Build dataset", "time_s": t_dataset},
    ])
    fig_times = px.bar(
        df_times,
        x="time_s",
        y="step",
        orientation="h",
        text=df_times["time_s"].round(0).astype(int).astype(str) + "s",
        color="step",
        color_discrete_sequence=PLOTLY_SEQUENCE[:5],
    )
    fig_times.update_traces(textposition="inside", insidetextanchor="middle", textfont=dict(color="white", size=13))
    fig_times.update_layout(**plotly_layout(
        height=350,
        showlegend=False,
        xaxis_title="Seconds",
        yaxis_title="",
    ))
    st.plotly_chart(fig_times, use_container_width=True)

with col_t2:
    total_recorded = t_discovery + t_issue_extraction + t_pr_linking + t_validation + t_dataset
    st.metric("Total recorded time", f"{total_recorded:.0f}s", f"≈ {total_recorded/60:.1f} min")

st.divider()

# Conversion rates.
st.subheader("Conversion rates between steps")

conv_rows = []
def add_conv(name, num, den):
    pct = (num / den * 100) if den > 0 else 0
    conv_rows.append({"step": name, "pct": pct, "detail": f"{num} / {den}"})

add_conv("Repos → Issues (avg per repo)", n_issues, n_repo)
conv_rows[-1]["pct"] = n_issues / n_repo if n_repo else 0
conv_rows[-1]["detail"] = f"{n_issues / n_repo:.1f} issues/repo" if n_repo else "—"

add_conv("Issues → with PR", n_with_pr, n_issues)
add_conv("With PR → AI validated", n_validate, n_with_pr)
add_conv("Validated → in dataset", n_dataset_done - n_dataset_fallite, n_validate)

df_conv = pd.DataFrame(conv_rows)

cc1, cc2, cc3, cc4 = st.columns(4)
for col, row in zip([cc1, cc2, cc3, cc4], conv_rows):
    with col:
        if row["step"] == "Repos → Issues (avg per repo)":
            col.metric(row["step"], row["detail"])
        else:
            col.metric(row["step"], f"{row['pct']:.1f}%", row["detail"])

st.divider()

# Run info.
st.subheader("Run info")

col_i1, col_i2 = st.columns(2)

with col_i1:
    st.markdown("**Step 1 · Discovery**")
    st.caption(
        f"Timestamp: `{disc_stats.get('timestamp', '—')}`  \n"
        f"Min stars: `{disc_stats.get('configuration', {}).get('min_stars', '—')}`  \n"
        f"Max repos/strategy: `{disc_stats.get('configuration', {}).get('max_repos_per_strategy', '—')}`  \n"
        f"Max total repos: `{disc_stats.get('configuration', {}).get('max_total_repos', '—')}`  \n"
        f"Languages: `{', '.join(disc_stats.get('configuration', {}).get('allowed_languages', []))}`"
    )

with col_i2:
    st.markdown("**Step 4 · AI Validation**")
    st.caption(
        f"Timestamp: `{vstats.get('timestamp', '—')}`  \n"
        f"Model: `{vstats.get('modello', '—')}`  \n"
        f"Temperature: `{vstats.get('configuration', {}).get('temperature', '—')}`  \n"
        f"Max body chars: `{vstats.get('configuration', {}).get('max_body_chars', '—')}`  \n"
        f"Checkpoint every: `{vstats.get('configuration', {}).get('checkpoint_every', '—')}`"
    )