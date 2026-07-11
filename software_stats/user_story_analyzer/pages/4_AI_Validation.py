"""
AI Validation page — analysis of pipeline step 4.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.ui import inject_css, page_hero, PLOTLY_SEQUENCE, PLOTLY_COLORS, plotly_layout
inject_css()

st.set_page_config(page_title="AI Validation", page_icon="🤖", layout="wide")

if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data

page_hero("🤖 AI Validation (step 4)", "Semantic filtering of issues via local LLM (Ollama).")

vstats = data.validation_stats
totals = vstats.get("totals", {})

# Top KPIs.
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Input issues", totals.get("input_issues", 0))
c2.metric("Validated", totals.get("validated_issues", 0))
c3.metric("Rejected", totals.get("discarded_issues", 0))
c4.metric("Validation rate", f"{totals.get('validation_rate_pct', 0):.1f}%")
c5.metric("Total time", f"{vstats.get('total_time_seconds', 0):.0f}s")

st.caption(
    f"**Model:** {vstats.get('modello', 'n/a')} · "
    f"**Temperature:** {vstats.get('configuration', {}).get('temperature', 'n/a')}"
)

st.divider()

# Funnel: input to validated/rejected.
st.subheader("Validation flow")

fig_funnel = go.Figure(go.Funnel(
    y=["Input (with PR)", "Validated", "Complete Connextra"],
    x=[
        totals.get("input_issues", 0),
        totals.get("validated_issues", 0),
        vstats.get("connextra_distribution", {}).get("complete", 0),
    ],
    textposition="inside",
    textinfo="value+percent previous",
    marker={"color": PLOTLY_SEQUENCE[:3]},
    connector={"line": {"color": "rgba(255,255,255,0.1)", "width": 1}},
))
fig_funnel.update_layout(**plotly_layout(height=300))
st.plotly_chart(fig_funnel, use_container_width=True)

st.divider()

# Connextra and confidence columns.
col_a, col_b = st.columns(2)

with col_a:
    st.subheader("Connextra completeness")
    conn = vstats.get("connextra_distribution", {})
    df_conn = pd.DataFrame([
        {"type": "Complete (with 'so that')", "count": conn.get("complete", 0)},
        {"type": "Partial (no 'so that')", "count": conn.get("partial", 0)},
    ])
    fig_conn = px.pie(
        df_conn,
        values="count",
        names="type",
        hole=0.5,
        color_discrete_sequence=[PLOTLY_COLORS["violet"], PLOTLY_COLORS["cyan"]],
    )
    fig_conn.update_traces(textinfo="value+percent", textfont=dict(color="white", size=14))
    fig_conn.update_layout(**plotly_layout(
        height=350,
        legend=dict(orientation="h", yanchor="bottom", y=-0.2, bgcolor="rgba(0,0,0,0)"),
    ))
    st.plotly_chart(fig_conn, use_container_width=True)

with col_b:
    st.subheader("Model confidence")
    conf = vstats.get("confidence_distribution", {})
    df_conf = pd.DataFrame([
        {"level": "High", "count": conf.get("high", 0)},
        {"level": "Medium", "count": conf.get("medium", 0)},
        {"level": "Low", "count": conf.get("low", 0)},
    ])
    fig_conf = px.bar(
        df_conf,
        x="level",
        y="count",
        text="count",
        color="level",
        color_discrete_map={
            "High": PLOTLY_COLORS["green"],
            "Medium": PLOTLY_COLORS["amber"],
            "Low": PLOTLY_COLORS["pink"],
        },
    )
    fig_conf.update_traces(textposition="outside")
    fig_conf.update_layout(**plotly_layout(
        height=350,
        showlegend=False,
        xaxis_title="",
        yaxis_title="Count",
    ))
    st.plotly_chart(fig_conf, use_container_width=True)

# Where the Connextra pattern is located.
st.divider()
st.subheader("Where the Connextra pattern is located")
st.caption(
    "Position of the Connextra pattern within the issue. "
    "Most valid user stories have the pattern in the body, "
    "less often in the title."
)

location_raw = vstats.get("location_distribution", {})
ubi_labels_map = {
    "title": "Title",
    "body": "Body",
    "both": "Both",
    "unspecified": "Unspecified",
}
df_ubi = pd.DataFrame([
    {"position": ubi_labels_map.get(k, k), "count": v}
    for k, v in location_raw.items()
])

if not df_ubi.empty:
    fig_ubi = px.bar(
        df_ubi,
        x="position",
        y="count",
        text="count",
        color="position",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig_ubi.update_traces(textposition="outside", textfont=dict(color="white", size=13))
    fig_ubi.update_layout(**plotly_layout(
        height=320,
        showlegend=False,
        xaxis_title="",
        yaxis_title="N° issues",
    ))
    st.plotly_chart(fig_ubi, use_container_width=True)

st.divider()

# Validated vs rejected by discovery strategy.
st.subheader("Validation outcome by discovery strategy")

val_per_strat = vstats.get("validated_per_discovery_strategy", {})
discard_per_strat = vstats.get("discarded_per_discovery_strategy", {})

all_strat = sorted(set(val_per_strat.keys()) | set(discard_per_strat.keys()))
df_strat = pd.DataFrame([
    {
        "strategy": s,
        "Validated": val_per_strat.get(s, 0),
        "Rejected": discard_per_strat.get(s, 0),
    }
    for s in all_strat
])
df_strat["total"] = df_strat["Validated"] + df_strat["Rejected"]
df_strat["rate_pct"] = (df_strat["Validated"] / df_strat["total"] * 100).round(1)

df_strat_melt = df_strat.melt(
    id_vars="strategy",
    value_vars=["Validated", "Rejected"],
    var_name="outcome",
    value_name="count",
)

fig_strat = px.bar(
    df_strat_melt,
    x="strategy",
    y="count",
    color="outcome",
    barmode="stack",
    text="count",
    color_discrete_map={
        "Validated": PLOTLY_COLORS["green"],
        "Rejected": PLOTLY_COLORS["pink"],
    },
)
fig_strat.update_layout(**plotly_layout(
    height=400,
    xaxis_title="",
    yaxis_title="Issues",
    legend_title="",
))
st.plotly_chart(fig_strat, use_container_width=True)

with st.expander("📋 Validation rate by strategy"):
    st.dataframe(
        df_strat[["strategy", "Validated", "Rejected", "total", "rate_pct"]]
            .rename(columns={"rate_pct": "Validation rate %"}),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# Metadata: Gherkin and acceptance criteria.
st.subheader("Metadata in validated issues")

meta = vstats.get("metadata_in_validated", {})
n_validate = totals.get("validated_issues", 1) or 1

col_m1, col_m2 = st.columns(2)
col_m1.metric(
    "With Acceptance Criteria",
    meta.get("with_acceptance_criteria", 0),
    f"{meta.get('with_acceptance_criteria', 0) / n_validate * 100:.1f}% of validated",
)
col_m2.metric(
    "With Gherkin (Given/When/Then)",
    meta.get("with_gherkin", 0),
    f"{meta.get('with_gherkin', 0) / n_validate * 100:.1f}% of validated",
)

# Average body length.
st.divider()
st.subheader("Average body length")
st.caption(
    "Average length in characters of the issue body. "
    "Rejected issues tend to be longer and more verbose — "
    "a hint that they are not well-formed user stories."
)

blen = vstats.get("body_length", {})
blen_validated = blen.get("validated_avg_chars", 0)
blen_rejected = blen.get("discarded_avg_chars", 0)
delta_char = blen_rejected - blen_validated

blen_c1, blen_c2 = st.columns(2)
blen_c1.metric("Validated (avg)", f"{blen_validated:.0f} char")
blen_c2.metric(
    "Rejected (avg)",
    f"{blen_rejected:.0f} char",
    delta=f"+{delta_char:.0f} char vs validated",
    delta_color="off",
)

df_blen = pd.DataFrame([
    {"group": "Validated", "avg_char": blen_validated},
    {"group": "Rejected", "avg_char": blen_rejected},
])
fig_blen = px.bar(
    df_blen,
    x="avg_char",
    y="group",
    orientation="h",
    text=df_blen["avg_char"].round(0).astype(int).astype(str) + " char",
    color="group",
    color_discrete_map={
        "Validated": PLOTLY_COLORS["cyan"],
        "Rejected": PLOTLY_COLORS["pink"],
    },
)
fig_blen.update_traces(
    textposition="inside", insidetextanchor="middle",
    textfont=dict(color="white", size=13),
)
fig_blen.update_layout(**plotly_layout(
    height=200,
    showlegend=False,
    xaxis_title="Characters (avg)",
    yaxis_title="",
))
st.plotly_chart(fig_blen, use_container_width=True)

# User-story linguistic patterns.
st.divider()
st.subheader("User-story linguistic patterns")
st.caption(
    "Analysis of roles ('As a ...') and action verbs ('I want to ...') "
    "extracted from the Connextra pattern."
)

top_roles = vstats.get("top_10_roles", [])
top_verbs = vstats.get("top_10_action_verbs", [])

col_r, col_v = st.columns(2)

with col_r:
    st.markdown("**Top 10 roles**")
    if top_roles:
        df_roles = pd.DataFrame(top_roles).sort_values("count", ascending=True)
        fig_roles = px.bar(
            df_roles,
            x="count",
            y="role",
            orientation="h",
            text="count",
            color="role",
            color_discrete_sequence=PLOTLY_SEQUENCE,
        )
        fig_roles.update_traces(
            textposition="inside", insidetextanchor="middle",
            textfont=dict(color="white", size=13),
        )
        fig_roles.update_layout(**plotly_layout(
            height=380,
            showlegend=False,
            xaxis_title="N° occurrences",
            yaxis_title="",
        ))
        st.plotly_chart(fig_roles, use_container_width=True)

with col_v:
    st.markdown("**Top 10 action verbs**")
    if top_verbs:
        df_verbs = pd.DataFrame(top_verbs).sort_values("count", ascending=True)
        fig_verbs = px.bar(
            df_verbs,
            x="count",
            y="verb",
            orientation="h",
            text="count",
            color="verb",
            color_discrete_sequence=PLOTLY_SEQUENCE,
        )
        fig_verbs.update_traces(
            textposition="inside", insidetextanchor="middle",
            textfont=dict(color="white", size=13),
        )
        fig_verbs.update_layout(**plotly_layout(
            height=380,
            showlegend=False,
            xaxis_title="N° occurrences",
            yaxis_title="",
        ))
        st.plotly_chart(fig_verbs, use_container_width=True)
        st.caption(
            "Some 'verbs' are normalization artifacts "
            "(e.g. 'i', 'the'): they signal features that do not start "
            "with a canonical verb."
        )

st.divider()

# Validated issues browser.
st.subheader("🔍 Validated issues browser")

issues_val = data.validated_issues
if not issues_val:
    st.info("No validated issue found.")
else:
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        strategies = sorted({i.get("strategy", "—") for i in issues_val})
        f_strat = st.multiselect("Strategy", strategies, default=strategies)
    with col_f2:
        f_conn = st.multiselect(
            "Connextra",
            ["complete", "partial"],
            default=["complete", "partial"],
        )
    with col_f3:
        f_conf = st.multiselect(
            "AI confidence",
            ["high", "medium", "low"],
            default=["high", "medium", "low"],
        )

    filtered = [
        i for i in issues_val
        if i.get("strategy") in f_strat
        and i.get("ai_validation", {}).get("connextra") in f_conn
        and i.get("ai_validation", {}).get("confidence") in f_conf
    ]

    st.caption(f"**{len(filtered)}** issues match the filters.")

    df_issues = pd.DataFrame([
        {
            "repo": i.get("repo", ""),
            "num": i.get("number", ""),
            "title": i.get("title", "")[:80],
            "strategy": i.get("strategy", ""),
            "connextra": i.get("ai_validation", {}).get("connextra", ""),
            "confidence": i.get("ai_validation", {}).get("confidence", ""),
            "pr_count": len(i.get("pr", [])),
            "url": i.get("url", ""),
        }
        for i in filtered
    ])

    st.dataframe(
        df_issues,
        use_container_width=True,
        hide_index=True,
        column_config={
            "url": st.column_config.LinkColumn("url", display_text="🔗"),
        },
    )