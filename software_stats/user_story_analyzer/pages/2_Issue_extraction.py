"""
Issue Extraction page — analysis of pipeline step 2.
"""
import pandas as pd
import plotly.express as px
import streamlit as st

from core.ui import inject_css, page_hero, PLOTLY_SEQUENCE, plotly_layout

inject_css()

st.set_page_config(page_title="Issue Extraction", page_icon="📬", layout="wide")

# Guard.
if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data

page_hero("📬 Issue Extraction (step 2)", "Analysis of candidate issue extraction from the discovered repositories.")

stats = data.issue_extraction_stats

if not stats:
    st.warning("`issue_extraction_stats` data not available.")
    st.stop()

totals = stats.get("totals", {})
config = stats.get("configuration", {})
per_strategy = stats.get("per_strategy", {})
dist = stats.get("issues_per_repo_distribution", {})
top10 = stats.get("top_10_repo_per_issue", [])
total_time = stats.get("total_time_seconds", 0)

# Top KPIs.
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Processed repos", totals.get("processed_repos", 0))
c2.metric("Issues found", totals.get("issues_found", 0))
c3.metric("Rejected (regex)", totals.get("discarded_regex", 0))
c4.metric("Empty repos", totals.get("empty_repos", 0))
c5.metric("Total time", f"{total_time:.0f}s", f"≈ {total_time / 60:.1f} min")

active_strategies = config.get("active_strategies", [])
st.caption(
    f"Config: max issues/repo = `{config.get('max_issues_per_repo', '—')}` · "
    f"search delay = `{config.get('search_delay_sec', '—')}s` · "
    f"active strategies = `{', '.join(active_strategies) if active_strategies else '—'}`"
)

st.divider()

# Issues found per strategy.
st.subheader("Issues found per strategy")

if per_strategy:
    df_strat = pd.DataFrame([
        {
            "strategy": name,
            "issues_found": s.get("issues_found", 0),
            "discarded_regex": s.get("discarded_regex", 0),
            "discard_rate_pct": s.get("regex_discard_rate_pct", 0),
            "time_s": s.get("time_seconds", 0),
            "avg_time_s_issue": s.get("avg_time_per_issue_seconds", 0),
        }
        for name, s in per_strategy.items()
    ])

    df_sorted = df_strat.sort_values("issues_found", ascending=True)
    fig1 = px.bar(
        df_sorted,
        x="issues_found",
        y="strategy",
        orientation="h",
        text="issues_found",
        color="strategy",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig1.update_traces(
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=13),
    )
    fig1.update_layout(**plotly_layout(
        height=320,
        showlegend=False,
        xaxis_title="Issues found",
        yaxis_title="",
    ))
    st.plotly_chart(fig1, use_container_width=True)

    st.divider()

    # Per-strategy efficiency.
    st.subheader("Per-strategy efficiency")

    col_left, col_right = st.columns(2)

    with col_left:
        df_t = df_strat.sort_values("time_s", ascending=True)
        fig2 = px.bar(
            df_t,
            x="time_s",
            y="strategy",
            orientation="h",
            text=df_t["time_s"].round(1).astype(str) + "s",
            color="strategy",
            color_discrete_sequence=PLOTLY_SEQUENCE,
        )
        fig2.update_traces(
            textposition="inside", insidetextanchor="middle",
            textfont=dict(color="white", size=13),
        )
        fig2.update_layout(**plotly_layout(
            height=320,
            showlegend=False,
            xaxis_title="Total time (s)",
            yaxis_title="",
            title="Time per strategy",
        ))
        st.plotly_chart(fig2, use_container_width=True)

    with col_right:
        df_m = df_strat.sort_values("avg_time_s_issue", ascending=True)
        fig3 = px.bar(
            df_m,
            x="avg_time_s_issue",
            y="strategy",
            orientation="h",
            text=df_m["avg_time_s_issue"].round(3).astype(str) + "s",
            color="strategy",
            color_discrete_sequence=PLOTLY_SEQUENCE,
        )
        fig3.update_traces(
            textposition="inside", insidetextanchor="middle",
            textfont=dict(color="white", size=13),
        )
        fig3.update_layout(**plotly_layout(
            height=320,
            showlegend=False,
            xaxis_title="Seconds per issue",
            yaxis_title="",
            title="Average cost (s/issue)",
        ))
        st.plotly_chart(fig3, use_container_width=True)

    st.divider()

    # Regex rejection rate per strategy.
    st.subheader("Regex rejection rate per strategy")

    df_discard = df_strat[df_strat["discard_rate_pct"] > 0].sort_values("discard_rate_pct", ascending=True)
    if not df_discard.empty:
        fig4 = px.bar(
            df_discard,
            x="discard_rate_pct",
            y="strategy",
            orientation="h",
            text=df_discard["discard_rate_pct"].round(1).astype(str) + "%",
            color="strategy",
            color_discrete_sequence=PLOTLY_SEQUENCE,
        )
        fig4.update_traces(
            textposition="inside", insidetextanchor="middle",
            textfont=dict(color="white", size=13),
        )
        fig4.update_layout(**plotly_layout(
            height=280,
            showlegend=False,
            xaxis_title="% issues rejected by client-side regex",
            yaxis_title="",
        ))
        st.plotly_chart(fig4, use_container_width=True)
        st.caption(
            "Only strategies with a client-side regex filter (e.g. TAG, CONNEXTRA) show a rejection rate. "
            "The others accept every issue returned by the API."
        )
    else:
        st.info("No strategy has a regex rejection rate > 0.")

    st.divider()

# Issues per repository distribution.
st.subheader("Issues per repository distribution")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Min", dist.get("min", "—"))
m2.metric("Max", dist.get("max", "—"))
m3.metric("Average", f"{dist.get('mean', 0):.1f}")
m4.metric("Median", dist.get("median", "—"))

st.caption(
    f"{dist.get('empty_repos', '—')} empty repos · "
    f"{dist.get('repo_con_almeno_1_issue', '—')} repos with at least 1 issue"
)

if data.issues:
    from collections import Counter
    conta_per_repo: Counter = Counter()
    for issue in data.issues:
        repo = issue.get("repo", "unknown")
        conta_per_repo[repo] += 1

    df_hist = pd.DataFrame({"issue_per_repo": list(conta_per_repo.values())})
    nbins = min(30, max(10, len(df_hist) // 5))
    fig5 = px.histogram(
        df_hist,
        x="issue_per_repo",
        nbins=nbins,
        color_discrete_sequence=[PLOTLY_SEQUENCE[0]],
    )
    fig5.update_layout(**plotly_layout(
        height=320,
        xaxis_title="Issues per repository",
        yaxis_title="N° repos",
        showlegend=False,
    ))
    st.plotly_chart(fig5, use_container_width=True)

st.divider()

# Top 10 repos.
st.subheader("Top 10 repos by number of issues")

if top10:
    df_top = pd.DataFrame(top10)
    df_top_sorted = df_top.sort_values("issue", ascending=True)
    fig6 = px.bar(
        df_top_sorted,
        x="issue",
        y="repo",
        orientation="h",
        text="issue",
        color_discrete_sequence=[PLOTLY_SEQUENCE[1]],
    )
    fig6.update_traces(
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=13),
    )
    fig6.update_layout(**plotly_layout(
        height=400,
        showlegend=False,
        xaxis_title="Issues found",
        yaxis_title="",
    ))
    st.plotly_chart(fig6, use_container_width=True)

    with st.expander("📋 Top 10 table with GitHub links"):
        df_top_link = df_top.copy()
        df_top_link["url"] = df_top_link["repo"].apply(
            lambda r: f"https://github.com/{r}"
        )
        st.dataframe(
            df_top_link,
            use_container_width=True,
            hide_index=True,
            column_config={
                "url": st.column_config.LinkColumn("GitHub link"),
            },
        )