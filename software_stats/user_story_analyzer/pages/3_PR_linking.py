"""
PR Linking page — analysis of pipeline step 3.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.ui import inject_css, page_hero, PLOTLY_SEQUENCE, plotly_layout

inject_css()

st.set_page_config(page_title="PR Linking", page_icon="🔗", layout="wide")

# Guard.
if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data

page_hero("🔗 PR Linking (step 3)", "Analysis of the link between candidate issues and Pull Requests on GitHub.")

stats = data.pr_linking_stats

if not stats:
    st.warning("`pr_linking_stats` data not available.")
    st.stop()

totals = stats.get("totals", {})
dist_pr = stats.get("prs_per_issue_distribution", {})
merge_time = stats.get("time_issue_to_merge", {})
top10_repo = stats.get("top_10_repo_per_pr", [])
top5_multi = stats.get("top_5_issue_multi_pr", [])
total_time = stats.get("total_time_seconds", 0)

n_input = totals.get("input_issues", len(data.issues))
n_with_pr = totals.get("issues_with_pr", len(data.issues_with_pr))
n_without_pr = totals.get("issues_without_pr", len(data.issues_without_pr))
success_rate = totals.get("success_rate_pct", (n_with_pr / n_input * 100) if n_input else 0)
tot_pr = totals.get("total_prs_found", 0)

# Top KPIs.
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Input issues", n_input)
c2.metric("With PR", n_with_pr)
c3.metric("Without PR", n_without_pr)
c4.metric("Success rate", f"{success_rate:.1f}%")
c5.metric("Total PRs found", tot_pr)

st.metric("Total time", f"{total_time:.0f}s", f"≈ {total_time / 60:.1f} min")

st.divider()

# Issue-to-PR funnel.
st.subheader("Issue → PR flow")
st.caption("How many of the candidate issues found at least one linked PR.")

stages = [
    ("Candidate issues (input)", n_input),
    ("With linked PR", n_with_pr),
    ("Without PR", n_without_pr),
]

fig_funnel = go.Figure(go.Funnel(
    y=[s[0] for s in stages],
    x=[s[1] for s in stages],
    textposition="inside",
    textinfo="value+percent initial",
    marker={"color": PLOTLY_SEQUENCE[:3]},
    connector={"line": {"color": "rgba(255,255,255,0.1)", "width": 1}},
))
fig_funnel.update_layout(**plotly_layout(height=320))
st.plotly_chart(fig_funnel, use_container_width=True)

st.divider()

# PRs per issue distribution.
st.subheader("PR per issue distribution")

st.metric("Total PRs found", tot_pr)

if dist_pr:
    buckets = ["1", "2", "3", "4", "5+"]
    values = [dist_pr.get(f, 0) for f in buckets]
    df_dist = pd.DataFrame({"pr_per_issue": buckets, "n_issue": values})

    fig_dist = px.bar(
        df_dist,
        x="pr_per_issue",
        y="n_issue",
        text="n_issue",
        color_discrete_sequence=[PLOTLY_SEQUENCE[2]],
    )
    fig_dist.update_traces(
        textposition="outside",
        textfont=dict(color="white", size=13),
    )
    fig_dist.update_layout(**plotly_layout(
        height=320,
        showlegend=False,
        xaxis_title="Linked PRs per issue",
        yaxis_title="N° issues",
    ))
    st.plotly_chart(fig_dist, use_container_width=True)

st.divider()

# Issue-to-merge time.
st.subheader("Issue → merge time (days)")

tm1, tm2, tm3, tm4 = st.columns(4)
tm1.metric("Min", f"{merge_time.get('min_days', '—')} d")
tm2.metric("Max", f"{merge_time.get('max_days', '—')} d")
tm3.metric("Average", f"{merge_time.get('mean_days', 0):.1f} d")
tm4.metric("Median", f"{merge_time.get('median_days', '—')} d")

sample_size = merge_time.get("sample_size", "—")
st.caption(f"Sample: {sample_size} issues with merged PR.")

st.divider()

# Top 10 repos by PRs found.
st.subheader("Top 10 repos by PRs found")

if top10_repo:
    df_top = pd.DataFrame(top10_repo)
    df_top_sorted = df_top.sort_values("prs_found", ascending=True)
    fig_top = px.bar(
        df_top_sorted,
        x="prs_found",
        y="repo",
        orientation="h",
        text="prs_found",
        color_discrete_sequence=[PLOTLY_SEQUENCE[3]],
    )
    fig_top.update_traces(
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=13),
    )
    fig_top.update_layout(**plotly_layout(
        height=420,
        showlegend=False,
        xaxis_title="PRs found",
        yaxis_title="",
    ))
    st.plotly_chart(fig_top, use_container_width=True)

    with st.expander("📋 Top 10 table"):
        st.dataframe(df_top, use_container_width=True, hide_index=True)

st.divider()

# Top 5 issues with the most PRs.
st.subheader("Top 5 issues with most linked PRs")

if top5_multi:
    df_multi = pd.DataFrame(top5_multi)
    st.dataframe(
        df_multi,
        use_container_width=True,
        hide_index=True,
        column_config={
            "url": st.column_config.LinkColumn("Issue link"),
            "repo": st.column_config.TextColumn("Repository"),
            "number": st.column_config.NumberColumn("N°", format="%d"),
            "title": st.column_config.TextColumn("Title"),
            "n_pr": st.column_config.NumberColumn("Linked PRs", format="%d"),
        },
    )