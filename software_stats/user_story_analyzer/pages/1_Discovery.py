"""
Discovery page — analysis of pipeline step 1.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.ui import inject_css, page_hero, PLOTLY_SEQUENCE, PLOTLY_COLORS, plotly_layout
inject_css()

st.set_page_config(page_title="Discovery", page_icon="🔎", layout="wide")

# Guard: is data loaded?
if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data

page_hero("🔎 Discovery (step 1)", "Analysis of the repo search strategies on GitHub.")

# Build the per-strategy DataFrame.
per_strategy = data.discovery_stats.get("per_strategy", {})
df_strat = pd.DataFrame([
    {
        "strategy": name,
        "accepted": s.get("accepted", 0),
        "discarded_stars": s.get("discarded_stars", 0),
        "discarded_date": s.get("discarded_date", 0),
        "discarded_language": s.get("discarded_language", 0),
        "discarded_regex": s.get("discarded_regex", 0),
        "duplicate_issues": s.get("duplicate_issues", 0),
        "issues_scanned": s.get("issues_scanned", 0),
        "time_s": s.get("time_seconds", 0),
    }
    for name, s in per_strategy.items()
])

if df_strat.empty:
    st.error("No per-strategy data found in run_stats.json.")
    st.stop()

# Top KPIs.
totals = data.discovery_stats.get("global_totals", {})
config = data.discovery_stats.get("configuration", {})

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Unique repos seen", totals.get("unique_repos_seen", 0))
c2.metric("Accepted repos", totals.get("accepted_repos", 0))
c3.metric("Rejected (regex)", totals.get("discarded_regex", 0))
c4.metric("Total time", f"{data.discovery_stats.get('total_time_seconds', 0):.0f}s")
c5.metric("Time windows", config.get("n_time_windows", "—"))

st.caption(
    "Each query is split by year to bypass GitHub Search API's 1000-result limit."
)

st.divider()

# Chart: accepted repos per strategy.
fig1 = px.bar(
    df_strat.sort_values("accepted", ascending=True),
    x="accepted",
    y="strategy",
    orientation="h",
    text="accepted",
    color="strategy",
    color_discrete_sequence=PLOTLY_SEQUENCE,
)
fig1.update_traces(textposition="inside", insidetextanchor="middle", textfont=dict(color="white", size=13))
fig1.update_layout(**plotly_layout(
    height=350,
    showlegend=False,
    xaxis_title="Accepted repos",
    yaxis_title="",
))
st.plotly_chart(fig1, use_container_width=True)

# Acceptance rate per strategy.
st.divider()
st.subheader("Acceptance rate per strategy")
st.caption(
    "Percentage of accepted repos over the total number of scanned issues. "
    "TEMPLATE has the highest rate because it starts from a strong signal "
    "(presence of structured templates)."
)

rates = data.discovery_stats.get("acceptance_rates_per_strategy_pct", {})
if rates:
    df_rates = pd.DataFrame([
        {"strategy": name, "rate_pct": val}
        for name, val in rates.items()
    ]).sort_values("rate_pct", ascending=True)

    fig_rates = px.bar(
        df_rates,
        x="rate_pct",
        y="strategy",
        orientation="h",
        text=df_rates["rate_pct"].round(2).astype(str) + "%",
        color="strategy",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig_rates.update_traces(
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=13),
    )
    fig_rates.update_layout(**plotly_layout(
        height=350,
        showlegend=False,
        xaxis_title="Acceptance rate (%)",
        yaxis_title="",
    ))
    st.plotly_chart(fig_rates, use_container_width=True)

# Chart: time per strategy and seconds per repo.
st.divider()
st.subheader("Time efficiency")

df_strat["s_per_repo"] = df_strat.apply(
    lambda r: r["time_s"] / r["accepted"] if r["accepted"] > 0 else 0, axis=1
)

col_left, col_right = st.columns(2)

with col_left:
    df_sorted = df_strat.sort_values("time_s", ascending=True)
    fig2 = px.bar(
        df_sorted,
        x="time_s",
        y="strategy",
        orientation="h",
        text=df_sorted["time_s"].round(0).astype(int).astype(str) + "s",
        color="strategy",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig2.update_traces(textposition="inside", insidetextanchor="middle", textfont=dict(color="white", size=13))
    fig2.update_layout(**plotly_layout(
        height=350,
        showlegend=False,
        xaxis_title="Total time (s)",
        yaxis_title="",
        title="Time per strategy",
    ))
    st.plotly_chart(fig2, use_container_width=True)

with col_right:
    df_sorted3 = df_strat.sort_values("s_per_repo", ascending=True)
    fig3 = px.bar(
        df_sorted3,
        x="s_per_repo",
        y="strategy",
        orientation="h",
        text=df_sorted3["s_per_repo"].round(2).astype(str),
        color="strategy",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig3.update_traces(textposition="inside", insidetextanchor="middle", textfont=dict(color="white", size=13))
    fig3.update_layout(**plotly_layout(
        height=350,
        showlegend=False,
        xaxis_title="Seconds per accepted repo",
        yaxis_title="",
        title="Average cost (s/repo)",
    ))
    st.plotly_chart(fig3, use_container_width=True)

# Chart: rejection reasons (stacked bars).
st.divider()
st.subheader("Rejection reasons per strategy")

df_melt = df_strat.melt(
    id_vars="strategy",
    value_vars=["discarded_stars", "discarded_language", "discarded_regex", "duplicate_issues"],
    var_name="motivo",
    value_name="count",
)
motivo_labels = {
    "discarded_stars": "Insufficient stars",
    "discarded_language": "Language not allowed",
    "discarded_regex": "Regex failed (GitHub false positive)",
    "duplicate_issues": "Repo already seen",
}
df_melt["motivo"] = df_melt["motivo"].map(motivo_labels)

fig4 = px.bar(
    df_melt,
    x="strategy",
    y="count",
    color="motivo",
    barmode="stack",
    color_discrete_sequence=PLOTLY_SEQUENCE,
)
fig4.update_layout(**plotly_layout(
    height=400,
    xaxis_title="",
    yaxis_title="Rejected issues",
    legend_title="",
))
st.plotly_chart(fig4, use_container_width=True)

# Characteristics of the discovered repositories.
st.divider()
st.subheader("Characteristics of discovered repositories")

dist_stelle = data.discovery_stats.get("stars_distribution", {})
dist_language = data.discovery_stats.get("language_distribution", {})
dist_anno = data.discovery_stats.get("creation_year_distribution", {})

col_sx, col_dx = st.columns(2)

# Stars distribution.
with col_sx:
    st.markdown("**Stars distribution**")
    ms1, ms2, ms3, ms4 = st.columns(4)
    ms1.metric("Min", f"{dist_stelle.get('min', '—'):,}" if isinstance(dist_stelle.get('min'), (int, float)) else "—")
    ms2.metric("Max", f"{dist_stelle.get('max', '—'):,}" if isinstance(dist_stelle.get('max'), (int, float)) else "—")
    ms3.metric("Average", f"{dist_stelle.get('mean', 0):,.0f}")
    ms4.metric("Median", f"{dist_stelle.get('median', '—'):,}" if isinstance(dist_stelle.get('median'), (int, float)) else "—")

    buckets_raw = dist_stelle.get("buckets", {})
    buckets_order = ["0-10", "10-100", "100-1k", "1k-10k", "10k+"]
    buckets_labels = [f for f in buckets_order if f in buckets_raw]
    buckets_values = [buckets_raw[f] for f in buckets_labels]

    if buckets_labels:
        df_buckets = pd.DataFrame({"band": buckets_labels, "repo": buckets_values})
        # Gradient palette from violet to pink.
        band_colors = [
            PLOTLY_COLORS["violet"],
            PLOTLY_COLORS["cyan"],
            PLOTLY_COLORS["pink"],
            PLOTLY_COLORS["amber"],
            PLOTLY_COLORS["green"],
        ]
        fig_stelle = px.bar(
            df_buckets,
            x="band",
            y="repo",
            text="repo",
            color="band",
            color_discrete_sequence=band_colors[:len(buckets_labels)],
        )
        fig_stelle.update_traces(textposition="outside", textfont=dict(color="white", size=13))
        fig_stelle.update_layout(**plotly_layout(
            height=320,
            showlegend=False,
            xaxis_title="Stars (bucket)",
            yaxis_title="N° repos",
        ))
        st.plotly_chart(fig_stelle, use_container_width=True)

# Language distribution.
with col_dx:
    st.markdown("**Language distribution**")
    if dist_language:
        languages = list(dist_language.keys())
        language_values = list(dist_language.values())

        color_map = {
            "Python": PLOTLY_COLORS["violet"],
            "Java": PLOTLY_COLORS["cyan"],
        }
        pie_colors = [
            color_map.get(l, PLOTLY_SEQUENCE[i % len(PLOTLY_SEQUENCE)])
            for i, l in enumerate(languages)
        ]

        fig_language = go.Figure(go.Pie(
            labels=languages,
            values=language_values,
            hole=0.5,
            marker={"colors": pie_colors},
            textinfo="value+percent",
            textfont={"color": "white", "size": 13},
        ))
        fig_language.update_layout(**plotly_layout(height=320))
        st.plotly_chart(fig_language, use_container_width=True)

# Creation-year distribution.
if dist_anno:
    years = sorted(dist_anno.keys())
    counts = [dist_anno[a] for a in years]
    df_anni = pd.DataFrame({"year": years, "repo": counts})

    fig_anni = px.bar(
        df_anni,
        x="year",
        y="repo",
        text="repo",
        color_discrete_sequence=[PLOTLY_COLORS["violet"]],
    )
    fig_anni.update_traces(textposition="outside", textfont=dict(color="white", size=12))
    fig_anni.update_layout(**plotly_layout(
        height=380,
        showlegend=False,
        xaxis_title="Creation year",
        yaxis_title="N° repos",
    ))
    st.plotly_chart(fig_anni, use_container_width=True)
    st.caption(
        "Repository creation year on GitHub — "
        "dominated by the recent years (growing trend in user story adoption)."
    )

# Raw data table.
st.divider()
with st.expander("📋 Raw per-strategy data"):
    st.dataframe(
        df_strat.style.format({
            "time_s": "{:.1f}",
            "s_per_repo": "{:.2f}",
        }),
        use_container_width=True,
        hide_index=True,
    )

# Top 10 repositories by stars.
st.divider()
st.subheader("Top 10 repositories by stars")
st.caption(
    "The most popular repositories in the dataset. They guarantee "
    "higher quality and visibility of the results."
)

top10 = data.discovery_stats.get("top_10_repo_by_stars", [])
if top10:
    df_top = pd.DataFrame(top10)
    df_top["url"] = df_top["name"].apply(lambda n: f"https://github.com/{n}")
    st.dataframe(
        df_top,
        use_container_width=True,
        hide_index=True,
        column_config={
            "name": st.column_config.TextColumn("Repository"),
            "stars": st.column_config.NumberColumn("Stars", format="%d"),
            "language": st.column_config.TextColumn("Language"),
            "url": st.column_config.LinkColumn("GitHub link"),
        },
    )