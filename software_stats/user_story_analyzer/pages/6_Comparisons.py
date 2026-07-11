"""
Comparisons & Overlap page — measures the overlap between discovery
strategies at different stages of the pipeline.
"""
from collections import Counter, defaultdict
from itertools import combinations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.ui import inject_css, page_hero, PLOTLY_SEQUENCE, plotly_layout
inject_css()

st.set_page_config(page_title="Comparisons & Overlap", page_icon="🔀", layout="wide")

if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data

page_hero("🔀 Comparisons & Overlap", "Overlap between discovery strategies along the pipeline.")

# Build three strategy-to-entity-set maps at different levels.

# Level 2: repos scanned in step 2 (a repo may be scanned by several
# strategies, since step 2 re-runs each strategy on every candidate).
repo_step2: dict[str, set[str]] = defaultdict(set)
for i in data.issues:
    strat = i.get("strategy")
    repo = i.get("repo")
    if strat and repo:
        repo_step2[strat].add(repo)

# Level 3: repos of issues validated in step 4.
repo_step4: dict[str, set[str]] = defaultdict(set)
for i in data.validated_issues:
    strat = i.get("strategy")
    repo = i.get("repo")
    if strat and repo:
        repo_step4[strat].add(repo)

LEVELS = {
    "Step 2 · Explored repos (pre-filter)": (repo_step2, "repo"),
    "Step 4 · Repos with AI-validated issues": (repo_step4, "repo"),
}

level_label = st.radio(
    "Analysis level",
    list(LEVELS.keys()),
    horizontal=False,
    help=(
        "**Step 2**: each strategy re-runs its queries on every candidate repo and the same "
        "repo may match patterns from different strategies. This is the true measure of strategy overlap.\n\n"
        "**Step 4**: after the LLM semantic filter, which strategies truly survive as "
        "producers of real user stories."
    ),
)

by_strat, entity_name = LEVELS[level_label]
strategies = sorted(by_strat.keys())

if not strategies:
    st.error("No strategy found at this level.")
    st.stop()

# Global KPIs.
all_entities = set().union(*by_strat.values())

entity_strat_count: dict[str, int] = defaultdict(int)
for s in strategies:
    for e in by_strat[s]:
        entity_strat_count[e] += 1

shared = sum(1 for c in entity_strat_count.values() if c >= 2)
exclusive = sum(1 for c in entity_strat_count.values() if c == 1)

c1, c2, c3, c4 = st.columns(4)
c1.metric(f"Unique {entity_name}s", len(all_entities))
c2.metric("Exclusive (1 strategy)", exclusive)
c3.metric("Shared (≥2 strategies)", shared)
c4.metric(
    "% shared",
    f"{(shared / len(all_entities) * 100) if all_entities else 0:.1f}%",
)

st.divider()

# Distribution.
st.subheader(f"Distribution: {entity_name}s found by N strategies")

dist = Counter(entity_strat_count.values())
df_dist = pd.DataFrame(
    [{"n_strategies": k, "count": v} for k, v in sorted(dist.items())]
)

fig_dist = px.bar(
    df_dist,
    x="n_strategies",
    y="count",
    text="count",
    color="n_strategies",
    color_continuous_scale=[[0, "#8B5CF6"], [0.5, "#06B6D4"], [1, "#EC4899"]],
)
fig_dist.update_traces(textposition="outside")
fig_dist.update_layout(**plotly_layout(
    height=320,
    showlegend=False,
    coloraxis_showscale=False,
    xaxis_title="Number of strategies that found the same entity",
    yaxis_title=f"N. {entity_name}s",
))
st.plotly_chart(fig_dist, use_container_width=True)

# Jaccard heatmap.
st.subheader("Similarity matrix (Jaccard)")
st.caption(
    "Jaccard = |A ∩ B| / |A ∪ B|. High values = strategies that find the same "
    f"{entity_name}s. Diagonal = 1.0 by construction."
)

matrix = np.zeros((len(strategies), len(strategies)))
abs_matrix = np.zeros((len(strategies), len(strategies)), dtype=int)
for i, s1 in enumerate(strategies):
    for j, s2 in enumerate(strategies):
        inter = len(by_strat[s1] & by_strat[s2])
        union = len(by_strat[s1] | by_strat[s2])
        matrix[i][j] = inter / union if union > 0 else 0.0
        abs_matrix[i][j] = inter

fig_heat = go.Figure(data=go.Heatmap(
    z=matrix,
    x=strategies,
    y=strategies,
    colorscale=[[0, "#0A0B0F"], [0.5, "#8B5CF6"], [1, "#EC4899"]],
    zmin=0, zmax=1,
    text=[[f"{v:.2f}" for v in row] for row in matrix],
    texttemplate="%{text}",
    textfont={"size": 14, "color": "white"},
    hovertemplate="<b>%{y}</b> ∩ <b>%{x}</b><br>Jaccard: %{z:.3f}<extra></extra>",
))
fig_heat.update_layout(**plotly_layout(
    height=450,
    xaxis=dict(side="bottom"),
    yaxis=dict(autorange="reversed"),
))
st.plotly_chart(fig_heat, use_container_width=True)

with st.expander(f"📋 Absolute co-occurrence (number of {entity_name}s in common)"):
    df_abs = pd.DataFrame(abs_matrix, index=strategies, columns=strategies)
    st.dataframe(df_abs, use_container_width=True)

st.divider()

# Ordered pairs.
st.subheader("Most overlapping strategy pairs")

pairs = []
for s1, s2 in combinations(strategies, 2):
    set1 = by_strat[s1]
    set2 = by_strat[s2]
    inter = len(set1 & set2)
    union = len(set1 | set2)
    if union == 0:
        continue
    pairs.append({
        "pair": f"{s1} ∩ {s2}",
        "in_common": inter,
        "total_union": union,
        "jaccard": inter / union,
    })

df_pairs = pd.DataFrame(pairs).sort_values("jaccard", ascending=False)

if df_pairs.empty or df_pairs["in_common"].sum() == 0:
    st.info(
        "No overlap between strategies at this level. "
        "Try changing the analysis level (Step 2 has the most real overlap)."
    )
else:
    top_k = df_pairs.head(10).sort_values("jaccard")
    fig_pairs = px.bar(
        top_k,
        x="jaccard",
        y="pair",
        orientation="h",
        text=top_k.apply(
            lambda r: f"{r['jaccard']:.3f} ({r["in_common"]} in common)", axis=1
        ),
        color="jaccard",
        color_continuous_scale=[[0, "#8B5CF6"], [1, "#EC4899"]],
    )
    fig_pairs.update_traces(textposition="inside", insidetextanchor="middle", textfont=dict(color="white", size=12))
    fig_pairs.update_layout(**plotly_layout(
        height=400,
        showlegend=False,
        coloraxis_showscale=False,
        xaxis_title="Jaccard",
        yaxis_title="",
    ))
    st.plotly_chart(fig_pairs, use_container_width=True)

    with st.expander("📋 All pairs"):
        st.dataframe(
            df_pairs.style.format({"jaccard": "{:.3f}"}),
            use_container_width=True,
            hide_index=True,
        )

st.divider()

# Most cross-cutting entities.
st.subheader(f"Most cross-cutting {entity_name}s")
st.caption(f"{entity_name.capitalize()}s found by the largest number of different strategies.")

most_shared = sorted(
    entity_strat_count.items(), key=lambda kv: kv[1], reverse=True
)
top = [(e, c) for e, c in most_shared if c >= 2][:20]

if not top:
    st.info(f"No {entity_name} shared between strategies at this level.")
else:
    entity_strats: dict[str, list[str]] = defaultdict(list)
    for s in strategies:
        for e in by_strat[s]:
            entity_strats[e].append(s)

    df_top = pd.DataFrame([
        {
            entity_name: e,
            "n_strategies": c,
            "found_by": ", ".join(sorted(entity_strats[e])),
        }
        for e, c in top
    ])
    st.dataframe(df_top, use_container_width=True, hide_index=True)