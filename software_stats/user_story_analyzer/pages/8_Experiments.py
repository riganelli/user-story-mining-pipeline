"""
Experiments page — comparison between multiple pipeline runs.

Session-independent: loads runs directly from the `experiments/` folder
and compares their results across all steps.

Four tabs:
  1. Config — diff between config.json files (what changes between experiments)
  2. Results — KPIs side by side at every step
  3. Strategies — % contribution of each strategy per experiment
  4. Overlap — how many repos/issues are shared (exclusion check)
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core.loader import list_experiments, load_output
from core.ui import inject_css, page_hero, PLOTLY_COLORS, PLOTLY_SEQUENCE, plotly_layout

inject_css()
st.set_page_config(page_title="Experiments", page_icon="🧪", layout="wide")

page_hero(
    "🧪 Experiment comparison",
    "Compare multiple pipeline runs: configurations, KPIs, strategy contribution, overlap.",
)


# Experiment selection (sidebar).

with st.sidebar:
    st.header("Selection")

    # Experiments path, two levels up (as in app.py).
    exp_root = st.text_input(
        "experiments/ folder",
        value=str(Path.cwd().parent.parent / "experiments"),
        key="cmp_exp_root",
    )

    available = list_experiments(exp_root)

    if not available:
        st.warning("No experiment found in this folder.")
        st.stop()

    # Default to experiments with step 5 completed.
    completed = [e["id"] for e in available if e["has_step5"]]
    incomplete = [e["id"] for e in available if not e["has_step5"]]

    sel_ids = st.multiselect(
        "Experiments to compare",
        options=[e["id"] for e in available],
        default=completed[:3] if len(completed) >= 2 else completed,
        help="Pick at least 2. ⚠️ next to the name = incomplete pipeline.",
        format_func=lambda x: f"{'✅' if x in completed else '⚠️'} {x}",
    )

    if incomplete:
        st.caption(f"⚠️ {len(incomplete)} experiment(s) with incomplete pipeline — some charts may not be available.")


if len(sel_ids) < 2:
    st.info("👈 Select **at least 2 experiments** from the sidebar to start the comparison.")
    st.stop()


# Cached data loading, to avoid reloading on every interaction.

@st.cache_data(show_spinner=True)
def load_many(exp_root: str, exp_ids: tuple[str, ...]) -> dict[str, dict]:
    """Load multiple experiments. Returns {exp_id: data_as_dict}.

    Returns a dict instead of PipelineOutput because dataclasses are not
    directly cacheable by Streamlit without extra configuration; we prefer
    to extract only the fields needed for reading.
    """
    out: dict[str, dict] = {}
    for eid in exp_ids:
        try:
            d = load_output(Path(exp_root) / eid)
            out[eid] = {
                "experiment_id": d.experiment_id,
                "config": d.config,
                "repo": d.repo,
                "discovery_stats": d.discovery_stats,
                "issues": d.issues,
                "issue_extraction_stats": d.issue_extraction_stats,
                "issues_with_pr": d.issues_with_pr,
                "issues_without_pr": d.issues_without_pr,
                "pr_linking_stats": d.pr_linking_stats,
                "validated_issues": d.validated_issues,
                "discarded_issues": d.discarded_issues,
                "validation_stats": d.validation_stats,
                "dataset_checkpoint": d.dataset_checkpoint,
                "dataset_stats": d.dataset_stats,
            }
        except Exception as e:
            out[eid] = {"_error": str(e)}
    return out


with st.spinner(f"Loading {len(sel_ids)} experiments..."):
    exps = load_many(exp_root, tuple(sel_ids))

errors = {eid: d["_error"] for eid, d in exps.items() if "_error" in d}
exps = {eid: d for eid, d in exps.items() if "_error" not in d}

if errors:
    for eid, err in errors.items():
        st.error(f"❌ {eid}: {err}")

if len(exps) < 2:
    st.warning("At least 2 successfully loaded experiments are required.")
    st.stop()


# Tabs.

tab_cfg, tab_kpi, tab_strat, tab_ovr = st.tabs([
    "⚙️ Configurations",
    "📊 Results",
    "🎯 Strategies",
    "🔗 Overlap",
])


# Tab 1: configurations side by side.

with tab_cfg:
    st.subheader("Configuration differences")
    st.caption("What changes between the selected experiments. Pink rows mark values that differ between columns.")

    rows = []

    def _add_row(label: str, getter):
        row = {"Parameter": label}
        for eid, d in exps.items():
            row[eid] = getter(d)
        rows.append(row)

    _add_row("Description", lambda d: d["config"].get("description", "—"))
    _add_row("Created at", lambda d: d["config"].get("created_at", "—")[:19] if d["config"].get("created_at") else "—")

    _add_row("Max total repos", lambda d: d["config"].get("discovery_config", {}).get("max_total_repos", "—"))
    _add_row("Max per strategy", lambda d: d["config"].get("discovery_config", {}).get("max_repos_per_strategy", "—"))
    _add_row("Min stars", lambda d: d["config"].get("discovery_config", {}).get("min_stars", "—"))
    _add_row("Languages", lambda d: ", ".join(d["config"].get("discovery_config", {}).get("languages", [])) or "—")
    _add_row("Active strategies", lambda d: ", ".join(d["config"].get("discovery_config", {}).get("active_strategies", [])) or "—")
    _add_row("Disabled strategies", lambda d: ", ".join(d["config"].get("discovery_config", {}).get("inactive_strategies", [])) or "—")
    _add_row("Excludes repos from", lambda d: ", ".join(d["config"].get("exclude_from_experiments", [])) or "—")
    _add_row("Excludes issues from (Phase 2)", lambda d: ", ".join(d["config"].get("exclude_issues_from_experiments", [])) or "—")
    _add_row("Excludes AI-rejected from", lambda d: ", ".join(d["config"].get("exclude_rejected_issues_from", [])) or "—")
    _add_row("Excludes validated from", lambda d: ", ".join(d["config"].get("exclude_validated_issues_from", [])) or "—")

    df_cfg = pd.DataFrame(rows)

    # Highlight rows whose values differ across columns.
    def _highlight_diff(row):
        vals = row.drop("Parameter").tolist()
        if len(set(map(str, vals))) > 1:
            return ["background-color: rgba(236, 72, 153, 0.15)"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df_cfg.style.apply(_highlight_diff, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    with st.expander("View the full config.json files"):
        for eid, d in exps.items():
            st.markdown(f"**{eid}**")
            st.json(d["config"])


# Tab 2: results side by side.

with tab_kpi:
    st.subheader("Pipelines side by side")
    st.caption("Key numbers for each pipeline step.")

    rows_kpi = []
    for eid, d in exps.items():
        validation_totals = d["validation_stats"].get("totals", {})
        ds_stats = d["dataset_stats"]
        ck_stats = d["dataset_checkpoint"].get("stats", {})

        rows_kpi.append({
            "Experiment": eid,
            "Repos (step 1)": len(d["repo"]),
            "Candidate issues (step 2)": len(d["issues"]),
            "Issues with PR (step 3)": len(d["issues_with_pr"]),
            "AI validated (step 4)": len(d["validated_issues"]),
            "AI rejected": len(d["discarded_issues"]),
            "Validation rate %": validation_totals.get("validation_rate_pct", 0),
            "Issues in dataset (step 5)": ck_stats.get("completed", 0),
            "Step 4 time (min)": round(d["validation_stats"].get("total_time_seconds", 0) / 60, 1),
        })

    df_kpi = pd.DataFrame(rows_kpi)
    st.dataframe(df_kpi, use_container_width=True, hide_index=True)

    st.divider()

    # Grouped bar chart of the main KPIs.
    st.subheader("Volume per step (bar chart)")

    metrics_to_plot = [
        ("Repos", "Repos (step 1)"),
        ("Cand. issues", "Candidate issues (step 2)"),
        ("With PR", "Issues with PR (step 3)"),
        ("Validated", "AI validated (step 4)"),
        ("In dataset", "Issues in dataset (step 5)"),
    ]

    plot_rows = []
    for _, row in df_kpi.iterrows():
        for short, full in metrics_to_plot:
            plot_rows.append({
                "Experiment": row["Experiment"],
                "Step": short,
                "Value": row[full],
            })
    df_plot = pd.DataFrame(plot_rows)

    # Keep the step order from metrics_to_plot.
    df_plot["Step"] = pd.Categorical(df_plot["Step"], categories=[s for s, _ in metrics_to_plot], ordered=True)
    df_plot = df_plot.sort_values("Step")

    fig_kpi = px.bar(
        df_plot,
        x="Step",
        y="Value",
        color="Experiment",
        barmode="group",
        text="Value",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig_kpi.update_traces(textposition="outside", textfont=dict(size=11))
    fig_kpi.update_layout(**plotly_layout(
        height=420,
        xaxis_title="",
        yaxis_title="N° items",
        legend_title="",
    ))
    st.plotly_chart(fig_kpi, use_container_width=True)

    st.divider()

    # Distribution of complete/partial Connextra per experiment.
    st.subheader("Connextra quality")
    st.caption(
        "Ratio between complete user stories (with 'so that') and partial ones. "
        "A stable ratio across experiments suggests that the pattern structure "
        "is a robust feature, not a sampling artifact."
    )

    conn_rows = []
    for eid, d in exps.items():
        dist = d["validation_stats"].get("connextra_distribution", {})
        complete_n = dist.get("complete", 0)
        partial_n = dist.get("partial", 0)
        total = complete_n + partial_n
        conn_rows.append({
            "Experiment": eid,
            "Complete": complete_n,
            "Partial": partial_n,
            "Total": total,
            "% Complete": round(complete_n * 100 / max(total, 1), 1),
        })

    df_conn = pd.DataFrame(conn_rows)

    cc1, cc2 = st.columns([3, 2])
    with cc1:
        df_conn_long = df_conn.melt(
            id_vars=["Experiment"],
            value_vars=["Complete", "Partial"],
            var_name="Type",
            value_name="Count",
        )
        fig_conn = px.bar(
            df_conn_long,
            x="Experiment",
            y="Count",
            color="Type",
            text="Count",
            color_discrete_map={"Complete": PLOTLY_COLORS["green"], "Partial": PLOTLY_COLORS["amber"]},
        )
        fig_conn.update_traces(textposition="inside", textfont=dict(color="white", size=12))
        fig_conn.update_layout(**plotly_layout(
            height=380,
            xaxis_title="",
            yaxis_title="N° user stories",
        ))
        st.plotly_chart(fig_conn, use_container_width=True)

    with cc2:
        st.markdown("**Complete Connextra rate**")
        for _, row in df_conn.iterrows():
            st.metric(row["Experiment"], f"{row['% Complete']}%", help=f"{row['Complete']} out of {row['Total']}")


# Tab 3: effect of the strategies.

with tab_strat:
    st.subheader("Strategy contribution to the final dataset")
    st.caption(
        "For each experiment: how many issues from each strategy made it "
        "to the final dataset (step 5). Disabled strategies appear as zero."
    )

    # One row per (experiment, strategy).
    strat_rows = []
    for eid, d in exps.items():
        # Count from dataset_stats, more reliable than raw discovery_stats.
        ips = d["dataset_stats"].get("issues_per_discovery_strategy", {})
        # Active strategies per config, so disabled ones show as zero.
        active = d["config"].get("discovery_config", {}).get("active_strategies", []) or list(ips.keys())
        all_strats = sorted(set(active) | set(ips.keys()))
        for s in all_strats:
            strat_rows.append({
                "Experiment": eid,
                "Strategy": s,
                "Issues in dataset": ips.get(s, 0),
                "Active": s in active,
            })

    df_strat = pd.DataFrame(strat_rows)

    if df_strat.empty:
        st.info("No strategy stats available. Make sure step 5 saved `_run_stats.json`.")
    else:
        fig_strat = px.bar(
            df_strat,
            x="Strategy",
            y="Issues in dataset",
            color="Experiment",
            barmode="group",
            text="Issues in dataset",
            color_discrete_sequence=PLOTLY_SEQUENCE,
        )
        fig_strat.update_traces(textposition="outside", textfont=dict(size=11))
        fig_strat.update_layout(**plotly_layout(
            height=420,
            xaxis_title="",
            yaxis_title="Issues in dataset",
            legend_title="",
        ))
        st.plotly_chart(fig_strat, use_container_width=True)

        # As a percentage per experiment, for a fair comparison across
        # runs of different sizes.
        st.subheader("% contribution per experiment")
        st.caption(
            "Same data expressed as a percentage of each experiment's dataset. "
            "Useful when experiments have different sizes: shows the *mix* of strategies."
        )

        totals_per_exp = df_strat.groupby("Experiment")["Issues in dataset"].transform("sum")
        df_strat["% in dataset"] = (df_strat["Issues in dataset"] / totals_per_exp.replace(0, 1) * 100).round(1)

        fig_pct = px.bar(
            df_strat,
            x="Experiment",
            y="% in dataset",
            color="Strategy",
            text="% in dataset",
            color_discrete_sequence=PLOTLY_SEQUENCE,
        )
        fig_pct.update_traces(textposition="inside", textfont=dict(color="white", size=11))
        fig_pct.update_layout(**plotly_layout(
            height=420,
            xaxis_title="",
            yaxis_title="% of dataset",
            legend_title="Strategy",
            yaxis=dict(range=[0, 100]),
        ))
        st.plotly_chart(fig_pct, use_container_width=True)

        with st.expander("Numeric detail"):
            df_pivot = df_strat.pivot(
                index="Strategy",
                columns="Experiment",
                values="Issues in dataset",
            ).fillna(0).astype(int)
            st.dataframe(df_pivot, use_container_width=True)


# Tab 4: overlap.

with tab_ovr:
    st.subheader("Overlap between experiments")
    st.caption(
        "How many repos or issues are shared between the selected experiments. "
        "If you used `exclude_from_experiments` correctly, the overlap **must** be zero."
    )

    level = st.radio(
        "Overlap level",
        ["Repos (step 1)", "Candidate issues (step 2)", "Validated issues (step 4)"],
        horizontal=True,
    )

    sets_per_exp: dict[str, set] = {}
    for eid, d in exps.items():
        if level == "Repos (step 1)":
            sets_per_exp[eid] = {r["name"] for r in d["repo"] if "name" in r}
        elif level == "Candidate issues (step 2)":
            sets_per_exp[eid] = {f"{i.get('repo')}#{i.get('number')}" for i in d["issues"]}
        else:
            sets_per_exp[eid] = {f"{i.get('repo')}#{i.get('number')}" for i in d["validated_issues"]}

    # Overlap matrix (Jaccard and absolute intersection).
    eids = list(sets_per_exp.keys())
    n = len(eids)

    matrix_inter = [[0] * n for _ in range(n)]
    matrix_jacc = [[0.0] * n for _ in range(n)]
    for i, a in enumerate(eids):
        for j, b in enumerate(eids):
            inter = len(sets_per_exp[a] & sets_per_exp[b])
            union = len(sets_per_exp[a] | sets_per_exp[b])
            matrix_inter[i][j] = inter
            matrix_jacc[i][j] = round(inter / union, 3) if union else 0.0

    cm1, cm2 = st.columns(2)

    with cm1:
        st.markdown("**Absolute intersection** (n° elements in common)")
        fig_inter = go.Figure(data=go.Heatmap(
            z=matrix_inter,
            x=eids,
            y=eids,
            text=matrix_inter,
            texttemplate="%{text}",
            colorscale=[[0, "#1F2937"], [1, PLOTLY_COLORS["violet"]]],
            showscale=True,
        ))
        fig_inter.update_layout(**plotly_layout(height=400))
        st.plotly_chart(fig_inter, use_container_width=True)

    with cm2:
        st.markdown("**Jaccard** (0 = no overlap, 1 = identical)")
        fig_jacc = go.Figure(data=go.Heatmap(
            z=matrix_jacc,
            x=eids,
            y=eids,
            text=[[f"{v:.2f}" for v in row] for row in matrix_jacc],
            texttemplate="%{text}",
            colorscale=[[0, "#1F2937"], [1, PLOTLY_COLORS["cyan"]]],
            showscale=True,
            zmin=0,
            zmax=1,
        ))
        fig_jacc.update_layout(**plotly_layout(height=400))
        st.plotly_chart(fig_jacc, use_container_width=True)

    # Shared elements, for pairs only.
    if n == 2:
        a, b = eids
        shared = sorted(sets_per_exp[a] & sets_per_exp[b])
        if shared:
            st.warning(
                f"⚠️ {len(shared)} elements are present in BOTH experiments. "
                f"If you expected zero overlap, check `exclude_from_experiments` in the config."
            )
            with st.expander(f"View the {len(shared)} shared elements"):
                # Cap at 200 to keep the page manageable.
                shown = shared[:200]
                st.code("\n".join(shown), language="text")
                if len(shared) > 200:
                    st.caption(f"... and {len(shared) - 200} more not shown.")
        else:
            st.success(
                f"✅ No overlap between **{a}** and **{b}** at level '{level}'. "
                f"The exclusions worked correctly."
            )