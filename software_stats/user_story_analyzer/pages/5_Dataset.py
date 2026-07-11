"""
Dataset page — exploration of pipeline step 5.
"""
import json
import re
from pathlib import Path

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

from core.ui import inject_css, page_hero, PLOTLY_SEQUENCE, PLOTLY_COLORS, plotly_layout
inject_css()

st.set_page_config(page_title="Dataset", page_icon="📦", layout="wide")

if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data
dataset_root = data.root / "5_dataset"

page_hero("📦 Final dataset (step 5)", "Browser of issues turned into a dataset with PR, diff and modified files.")


# Helpers.
# File-language classification constants, aligned with 5_build_dataset.py.
PY_EXTS   = {".py", ".cfg", ".toml", ".ini"}
JAVA_EXTS = {".java", ".xml", ".gradle", ".properties", ".kts"}


@st.cache_data(show_spinner=False, ttl=3600)
def _is_image_accessible(url: str, timeout: float = 3.0) -> bool:
    """Check via HEAD that the URL is reachable and returns an image.

    Cached for 1h: avoids repeating the HEAD on URLs already checked during
    the same session (useful when reopening the same expander).
    """
    try:
        r = httpx.head(url, timeout=timeout, follow_redirects=True)
        if r.status_code >= 400:
            return False
        ctype = r.headers.get("content-type", "").lower()
        # Try anyway when content-type is missing but HEAD is OK.
        return "image" in ctype or ctype == ""
    except Exception:
        return False


def _render_issue_body(body: str) -> None:
    """Render the body of a GitHub issue handling <img> tags correctly.

    GitHub allows raw HTML in bodies. Streamlit does not execute HTML in markdown,
    so <img> tags would show up as text. Here we extract them, remove them
    from the markdown and show only actually reachable images.
    For URLs that aren't accessible (e.g. Zenhub auth-protected) we show a link.
    """
    if not body or not body.strip():
        st.markdown("_(no body)_")
        return

    # Extract the src of every <img> tag.
    img_pattern = re.compile(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*/?>', re.IGNORECASE)
    img_urls = img_pattern.findall(body)

    # Remove <img> tags so they do not render as raw text.
    body_clean = img_pattern.sub("", body)

    st.markdown(body_clean)

    # Check each image is reachable before calling st.image.
    for url in img_urls:
        if _is_image_accessible(url):
            st.image(url, use_container_width=True)
        else:
            st.caption(
                f"🖼️ Image not accessible (probably requires authentication): "
                f"[open link]({url})"
            )


@st.cache_data(show_spinner=False)
def scan_dataset(root_str: str) -> pd.DataFrame:
    """Scan the 5_dataset/ folder and return a DataFrame with one row per issue."""
    root = Path(root_str)
    rows = []
    for lang_dir in root.iterdir():
        if not lang_dir.is_dir() or lang_dir.name.startswith("_"):
            continue
        for repo_dir in lang_dir.iterdir():
            if not repo_dir.is_dir():
                continue
            for issue_dir in repo_dir.iterdir():
                if not issue_dir.is_dir() or not issue_dir.name.startswith("issue_"):
                    continue
                data_file = issue_dir / "data.json"
                if not data_file.exists():
                    continue
                try:
                    with data_file.open("r", encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue

                pr = d.get("primary_pr") or {}
                ai = d.get("ai_validation") or {}
                diff = d.get("diff_between_base_and_last") or []

                # Classify the modified-file extensions as python, java or
                # other, for the filters.
                _exts = set()
                for _f in diff:
                    _name = (_f.get("filename") or "").lower()
                    if "." in _name:
                        _ext = "." + _name.rsplit(".", 1)[1]
                        _exts.add(_ext)
                _has_py    = any(e in PY_EXTS    for e in _exts)
                _has_java  = any(e in JAVA_EXTS  for e in _exts)
                _has_other = any(e not in PY_EXTS and e not in JAVA_EXTS for e in _exts) if _exts else False

                # Dominant filter label: the main language actually touched.
                if _has_py and not _has_java:
                    _file_lang = "python"
                elif _has_java and not _has_py:
                    _file_lang = "java"
                elif _has_py and _has_java:
                    _file_lang = "mixed"
                elif _has_other:
                    _file_lang = "other"
                else:
                    _file_lang = "none"

                rows.append({
                    "lang": lang_dir.name,
                    "repo": d.get("repository", ""),
                    "issue_num": d.get("issue_number", 0),
                    "title": d.get("issue_title", ""),
                    "strategy": d.get("discovery_strategy", ""),
                    "connextra": ai.get("connextra", ""),
                    "confidence": ai.get("confidence", ""),
                    "pr_num": pr.get("number", ""),
                    "pr_additions": pr.get("additions", 0),
                    "pr_deletions": pr.get("deletions", 0),
                    "pr_changed_files": pr.get("changed_files", 0),
                    "n_files_diff": len(diff),
                    "file_lang": _file_lang,
                    "has_python_files": _has_py,
                    "has_java_files":   _has_java,
                    "has_other_files":  _has_other,
                    "extensions": sorted(_exts),
                    "issue_url": d.get("issue_url", ""),
                    "files_complete": d.get("files_complete", True),
                    "files_capped":   d.get("files_capped", False),
                    "files_fetched":  d.get("files_fetched", len(diff)),
                    "_path": str(issue_dir),
                })
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False)
def load_issue_data(issue_path: str) -> dict:
    with (Path(issue_path) / "data.json").open("r", encoding="utf-8") as f:
        return json.load(f)


# Top KPIs.
ck_stats = data.dataset_checkpoint.get("stats", {})
dstats = data.dataset_stats
df = scan_dataset(str(dataset_root))

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Issues in dataset", len(df))
c2.metric("Unique repos", df["repo"].nunique() if not df.empty else 0)
c3.metric("Completed (last run)", ck_stats.get("completed", 0))
c4.metric("Skipped (cache)", ck_stats.get("skipped_checkpoint", 0))
c5.metric("Failed", ck_stats.get("failed", 0))
c6.metric(
    "Truncated file lists",
    int((~df["files_complete"]).sum()) if not df.empty and "files_complete" in df else 0,
    help="Issues whose PR file list is incomplete: capped at the per-PR limit, "
         "or partial because a GitHub /files page failed. SHA + fetch URLs are "
         "still in data.json for the files that were captured.",
)

if df.empty:
    st.error("No issue found in the dataset. Make sure `5_dataset/` contains `java/` or `python/`.")
    st.stop()

st.divider()

# Overview charts.
col_a, col_b = st.columns(2)

with col_a:
    st.subheader("Issues per language")
    df_lang = df.groupby("lang", as_index=False).size().rename(columns={"size": "issue"})
    df_lang["repo"] = df.groupby("lang")["repo"].nunique().values
    fig_lang = px.bar(
        df_lang,
        x="lang",
        y="issue",
        text="issue",
        color="lang",
        color_discrete_sequence=[PLOTLY_COLORS["violet"], PLOTLY_COLORS["cyan"]],
    )
    fig_lang.update_traces(textposition="outside")
    fig_lang.update_layout(**plotly_layout(
        height=320, showlegend=False, xaxis_title="", yaxis_title="Issues",
    ))
    st.plotly_chart(fig_lang, use_container_width=True)

with col_b:
    st.subheader("Modified files per issue (distribution)")
    fig_files = px.histogram(
        df,
        x="n_files_diff",
        nbins=30,
        color_discrete_sequence=[PLOTLY_COLORS["violet"]],
    )
    fig_files.update_layout(**plotly_layout(
        height=320,
        xaxis_title="N. files in diff",
        yaxis_title="Issues",
    ))
    st.plotly_chart(fig_files, use_container_width=True)

# Primary language vs file language.
# Compares the repo primary language declared by GitHub with the language
# of the files actually modified by the issues; on polyglot repos the two
# distributions diverge.
st.divider()
st.subheader("🌐 Primary language vs language of modified files")
st.caption(
    "Comparison between the **primaryLanguage** declared by GitHub (the folder "
    "the pipeline placed the repo in) and the language of the **files actually "
    "touched** by the PRs. On polyglot repos the two views diverge, and this "
    "difference is what justifies an extension-based filter over a binary "
    "language filter."
)

_lang_pct_col1, _lang_pct_col2 = st.columns(2)

with _lang_pct_col1:
    st.markdown("**By repo primary language**")
    _df_pl = (
        df.groupby("lang", as_index=False).size()
        .rename(columns={"size": "issue"})
        .sort_values("issue", ascending=False)
    )
    _tot_pl = _df_pl["issue"].sum()
    _df_pl["pct"] = (_df_pl["issue"] * 100 / max(_tot_pl, 1)).round(1)
    fig_pl = px.pie(
        _df_pl, names="lang", values="issue",
        color_discrete_sequence=PLOTLY_SEQUENCE,
        hole=0.4,
    )
    fig_pl.update_traces(textinfo="label+percent")
    fig_pl.update_layout(**plotly_layout(height=300, showlegend=False))
    st.plotly_chart(fig_pl, use_container_width=True)
    for _, _r in _df_pl.iterrows():
        st.caption(f"  · `{_r['lang']}`: **{int(_r['issue'])}** issues ({_r['pct']}%)")

with _lang_pct_col2:
    st.markdown("**By language of modified files**")
    _df_fl = (
        df.groupby("file_lang", as_index=False).size()
        .rename(columns={"size": "issue"})
        .sort_values("issue", ascending=False)
    )
    _tot_fl = _df_fl["issue"].sum()
    _df_fl["pct"] = (_df_fl["issue"] * 100 / max(_tot_fl, 1)).round(1)
    fig_fl = px.pie(
        _df_fl, names="file_lang", values="issue",
        color_discrete_sequence=PLOTLY_SEQUENCE,
        hole=0.4,
    )
    fig_fl.update_traces(textinfo="label+percent")
    fig_fl.update_layout(**plotly_layout(height=300, showlegend=False))
    st.plotly_chart(fig_fl, use_container_width=True)
    for _, _r in _df_fl.iterrows():
        st.caption(f"  · `{_r['file_lang']}`: **{int(_r['issue'])}** issues ({_r['pct']}%)")

# Coverage: how many issues from "java" repos touch Python files, and
# vice versa.
_n_total = len(df)
_n_java_repo_py_files   = int(((df["lang"] == "java") & df["has_python_files"]).sum())
_n_python_repo_java_files = int(((df["lang"] == "python") & df["has_java_files"]).sum())
_n_files_other = int(df["has_other_files"].sum())

_mc1, _mc2, _mc3 = st.columns(3)
_mc1.metric(
    "Java repos touching Python files",
    _n_java_repo_py_files,
    help="Issues on repos with primaryLanguage=Java that modify at least one Python file. "
         "These are false negatives a binary filter on primaryLanguage would miss.",
)
_mc2.metric(
    "Python repos touching Java files",
    _n_python_repo_java_files,
    help="Issues on repos with primaryLanguage=Python that modify at least one Java file.",
)
_mc3.metric(
    "Issues with 'other' files",
    _n_files_other,
    help="Issues that touch at least one file with an extension that is neither Python "
         "nor Java (e.g. .md, .yml, .json, .ts...).",
)

# Issues per discovery strategy.
st.divider()
st.subheader("Issues per discovery strategy")
st.caption(
    "Which discovery strategy the issues that ended up in the dataset come from. "
    "Lets you assess each strategy's yield through to the end of the pipeline."
)

ips = dstats.get("issues_per_discovery_strategy", {})
if ips:
    df_ips = pd.DataFrame([
        {"strategy": k, "issue": v} for k, v in ips.items()
    ]).sort_values("issue", ascending=True)

    fig_ips = px.bar(
        df_ips,
        x="issue",
        y="strategy",
        orientation="h",
        text="issue",
        color="strategy",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig_ips.update_traces(
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=13),
    )
    fig_ips.update_layout(**plotly_layout(
        height=320,
        showlegend=False,
        xaxis_title="Issues in dataset",
        yaxis_title="",
    ))
    st.plotly_chart(fig_ips, use_container_width=True)

# Top repos
st.divider()
st.subheader("Top repos by number of issues")
df_top = (
    df.groupby(["repo", "lang"], as_index=False)
    .size()
    .rename(columns={"size": "issue"})
    .sort_values("issue", ascending=False)
    .head(15)
)
fig_top = px.bar(
    df_top.sort_values("issue"),
    x="issue",
    y="repo",
    orientation="h",
    color="lang",
    text="issue",
    color_discrete_map={"java": PLOTLY_COLORS["violet"], "python": PLOTLY_COLORS["cyan"]},
)
fig_top.update_traces(textposition="inside", insidetextanchor="end", textfont=dict(color="white", size=12))
fig_top.update_layout(**plotly_layout(
    height=450, xaxis_title="Issues", yaxis_title="",
    legend_title="",
))
st.plotly_chart(fig_top, use_container_width=True)

# Pull-request size.
st.divider()
st.subheader("📏 Pull-request size")
st.caption("Size statistics for the Pull Requests linked to the user stories.")

dist_files = dstats.get("distribution_files_per_pr", {})
dist_add = dstats.get("distribution_additions_per_pr", {})
dist_del = dstats.get("distribution_deletions_per_pr", {})


def _metric_row(label: str, d: dict):
    st.markdown(f"**{label}**")
    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Min", d.get("min", "—"))
    mc2.metric("Max", d.get("max", "—"))
    mc3.metric("Mean", f"{d.get('mean', 0):.1f}")
    mc4.metric("Median", d.get("median", "—"))


_metric_row("Modified files per PR", dist_files)
_metric_row("Lines added (additions)", dist_add)
_metric_row("Lines removed (deletions)", dist_del)

dist_diff = dstats.get("distribution_diff_size", {})
if dist_diff:
    buckets_order = ["<10", "10-100", "100-500", "500-1000", "1000+"]
    buckets_labels = [f for f in buckets_order if f in dist_diff]
    buckets_values = [dist_diff[f] for f in buckets_labels]

    df_diff = pd.DataFrame({"band": buckets_labels, "pr": buckets_values})
    diff_colors = [
        PLOTLY_COLORS["violet"],
        PLOTLY_COLORS["cyan"],
        PLOTLY_COLORS["amber"],
        PLOTLY_COLORS["pink"],
        PLOTLY_COLORS["muted"],
    ]
    fig_diff = px.bar(
        df_diff,
        x="band",
        y="pr",
        text="pr",
        color="band",
        color_discrete_sequence=diff_colors[:len(buckets_labels)],
    )
    fig_diff.update_traces(textposition="outside", textfont=dict(color="white", size=13))
    fig_diff.update_layout(**plotly_layout(
        height=350,
        showlegend=False,
        xaxis_title="Diff size (additions + deletions)",
        yaxis_title="N° PRs",
    ))
    st.plotly_chart(fig_diff, use_container_width=True)
    st.caption(
        "Total additions + deletions per PR. "
        "Most PRs are medium-sized (100-500 lines)."
    )

# Top 20 modified file extensions.
st.divider()
st.subheader("Top 20 modified file extensions")
st.caption(
    "The file extensions most often touched by the PRs. "
    "Reflects the dataset languages (Python and Java) and their ecosystem "
    "of configuration and documentation files."
)

top20_ext = dstats.get("top_20_file_extensions", [])
if top20_ext:
    df_ext = pd.DataFrame(top20_ext).sort_values("count", ascending=True)
    fig_ext = px.bar(
        df_ext,
        x="count",
        y="extension",
        orientation="h",
        text="count",
        color="extension",
        color_discrete_sequence=PLOTLY_SEQUENCE,
    )
    fig_ext.update_traces(
        textposition="inside", insidetextanchor="middle",
        textfont=dict(color="white", size=13),
    )
    fig_ext.update_layout(**plotly_layout(
        height=500,
        showlegend=False,
        xaxis_title="N° files",
        yaxis_title="",
    ))
    st.plotly_chart(fig_ext, use_container_width=True)

st.divider()

# Issue browser: a single selectbox view.

_CONN_EMOJI = {"complete": "✅ complete", "partial": "🟡 partial"}
_CONF_EMOJI = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}

st.subheader("🔍 Explore by repository")

# Global filters, including modified-file language.
_fc1, _fc2, _fc3, _fc4 = st.columns(4)
with _fc1:
    _f_lang = st.multiselect(
        "Repo primary language", sorted(df["lang"].unique()),
        default=sorted(df["lang"].unique()), key="ds_f_lang",
        help="Language declared by the repo on GitHub (java/ or python/ folder).",
    )
with _fc2:
    _f_file_lang = st.multiselect(
        "Modified files language", sorted(df["file_lang"].unique()),
        default=sorted(df["file_lang"].unique()), key="ds_f_file_lang",
        help="Language of the files actually touched by the PR. "
             "'mixed' = both .py and .java; 'other' = only other extensions; "
             "'none' = empty/binary diff.",
    )
with _fc3:
    _f_strat = st.multiselect(
        "Discovery strategy", sorted(df["strategy"].unique()),
        default=sorted(df["strategy"].unique()), key="ds_f_strat",
    )
with _fc4:
    _f_text = st.text_input(
        "Search in repo name", "", key="ds_f_search",
        placeholder="e.g. gcivil, hiero, IBM...",
    )

_df_filtered = df[
    df["lang"].isin(_f_lang)
    & df["file_lang"].isin(_f_file_lang)
    & df["strategy"].isin(_f_strat)
]
if _f_text:
    _df_filtered = _df_filtered[_df_filtered["repo"].str.contains(_f_text, case=False, na=False)]

_repo_summary = (
    _df_filtered.groupby("repo")
    .agg(n_issue=("issue_num", "count"), lang=("lang", "first"))
    .reset_index()
    .sort_values("n_issue", ascending=False)
)

if _repo_summary.empty:
    st.info("No repository matches the filters. Try broadening the selection.")
    st.stop()

_repo_summary["_label"] = _repo_summary.apply(
    lambda r: f"{r['repo']}  ({int(r['n_issue'])} issues · {r['lang']})", axis=1
)
_label_to_repo = dict(zip(_repo_summary["_label"], _repo_summary["repo"]))

_sel_col, _link_col = st.columns([5, 1])
with _sel_col:
    _sel_label = st.selectbox(
        f"Pick a repository  ·  {len(_repo_summary)} available",
        _repo_summary["_label"].tolist(),
        key="ds_repo_select",
    )
with _link_col:
    st.markdown("&nbsp;")
    if _sel_label:
        st.link_button(
            "🔗 GitHub",
            f"https://github.com/{_label_to_repo[_sel_label]}",
            use_container_width=True,
        )

if not _sel_label:
    st.stop()

sel_repo = _label_to_repo[_sel_label]
_df_repo = df[df["repo"] == sel_repo].copy()

st.divider()

_sm1, _sm2, _sm3, _sm4 = st.columns(4)
_sm1.metric("Issues in dataset", len(_df_repo))
_sm2.metric("Total additions", int(_df_repo["pr_additions"].sum()))
_sm3.metric("Total deletions", int(_df_repo["pr_deletions"].sum()))
_sm4.metric("Modified files (total)", int(_df_repo["pr_changed_files"].sum()))

_badge_map = {"LABEL": "🟣", "TEMPLATE": "🔵", "TAG": "🟠", "MENTION": "🟡", "CONNEXTRA": "🟢"}
_badges = " · ".join(
    f"{_badge_map.get(s, '⚪')} `{s}`" for s in sorted(_df_repo["strategy"].unique())
)
st.caption(f"Discovery strategies: {_badges}")

# Modified-file language distribution for this repo.
_repo_primary = _df_repo["lang"].iloc[0] if not _df_repo.empty else "—"
_df_lang_repo = (
    _df_repo.groupby("file_lang", as_index=False).size()
    .rename(columns={"size": "issue"})
    .sort_values("issue", ascending=False)
)
_tot_repo = _df_lang_repo["issue"].sum()
_df_lang_repo["pct"] = (_df_lang_repo["issue"] * 100 / max(_tot_repo, 1)).round(1)

st.markdown(
    f"**📊 Modified-file languages in this repo** "
    f"(GitHub primary: `{_repo_primary}`)"
)
_lang_caption_parts = []
for _, _r in _df_lang_repo.iterrows():
    _lang_caption_parts.append(
        f"`{_r['file_lang']}`: **{int(_r['issue'])}** ({_r['pct']}%)"
    )
st.caption(" · ".join(_lang_caption_parts))

st.divider()

# Repo-internal filters, including file language.
_if1, _if2, _if3, _if4 = st.columns(4)
with _if1:
    _fi_conn = st.multiselect(
        "Connextra", sorted(_df_repo["connextra"].unique()),
        default=sorted(_df_repo["connextra"].unique()), key=f"l2_conn_{sel_repo}",
    )
with _if2:
    _fi_conf = st.multiselect(
        "AI confidence", sorted(_df_repo["confidence"].unique()),
        default=sorted(_df_repo["confidence"].unique()), key=f"l2_conf_{sel_repo}",
    )
with _if3:
    _fi_strat = st.multiselect(
        "Discovery strategy", sorted(_df_repo["strategy"].unique()),
        default=sorted(_df_repo["strategy"].unique()), key=f"l2_strat_{sel_repo}",
    )
with _if4:
    _fi_file_lang = st.multiselect(
        "Modified-file language", sorted(_df_repo["file_lang"].unique()),
        default=sorted(_df_repo["file_lang"].unique()),
        key=f"l2_file_lang_{sel_repo}",
        help="Filter this repo's issues by the language of the files actually touched.",
    )

_df_filt2 = _df_repo[
    _df_repo["connextra"].isin(_fi_conn)
    & _df_repo["confidence"].isin(_fi_conf)
    & _df_repo["strategy"].isin(_fi_strat)
    & _df_repo["file_lang"].isin(_fi_file_lang)
].sort_values("issue_num", ascending=False).reset_index(drop=True)

st.caption(f"**{len(_df_filt2)}** issues of {sel_repo} match the filters.")

for _, _row in _df_filt2.iterrows():
    _num = int(_row["issue_num"])
    _title = (_row["title"] or "")[:80]
    _conn_label = _CONN_EMOJI.get(_row["connextra"], _row["connextra"])
    _conf_label = _CONF_EMOJI.get(_row["confidence"], _row["confidence"])
    _exp_label = f"#{_num} — {_title}  ·  {_conn_label}  ·  {_conf_label}"

    with st.expander(_exp_label, expanded=False):
        try:
            _issue_data = load_issue_data(_row["_path"])
        except Exception as _e:
            st.error(f"Cannot load data.json: {_e}")
            continue

        _lnk_col, _cap_col = st.columns([2, 5])
        with _lnk_col:
            _issue_url = _issue_data.get("issue_url", "")
            if _issue_url:
                st.link_button("🔗 Open issue on GitHub", _issue_url)
        with _cap_col:
            st.caption(
                f"Strategy: {_issue_data.get('discovery_strategy', '—')} · "
                f"State: {_issue_data.get('issue_state', '—')} · "
                f"Created: {_issue_data.get('issue_created_at', '—')} · "
                f"Closed: {_issue_data.get('issue_closed_at', '—')}"
            )
            # Extensions actually touched by the PR.
            _row_exts = _row.get("extensions", []) or []
            if _row_exts:
                st.caption(
                    f"📁 Modified files ({_row['file_lang']}): "
                    + " · ".join(f"`{e}`" for e in _row_exts)
                )

        st.markdown("---")

        st.markdown("#### 📝 Body")
        _body = _issue_data.get("issue_body", "") or ""
        _render_issue_body(_body)
        _labels = _issue_data.get("issue_labels", [])
        if _labels:
            st.caption("**Labels:** " + " · ".join(f"`{l}`" for l in _labels))

        st.markdown("---")

        st.markdown("#### 🤖 AI validation")
        _ai = _issue_data.get("ai_validation") or {}
        _meta = _ai.get("metadata") or {}
        _ai_c1, _ai_c2 = st.columns(2)
        with _ai_c1:
            st.markdown(f"**Connextra:** {_ai.get('connextra', '—')}")
            st.markdown(f"**Role:** {_ai.get('role', '—')}")
            st.markdown(f"**Feature:** {_ai.get('feature', '—')}")
            st.markdown(f"**Benefit:** {_ai.get('benefit') or '—'}")
        with _ai_c2:
            st.markdown(f"**Location:** {_ai.get('location', '—')}")
            st.markdown(f"**Confidence:** {_ai.get('confidence', '—')}")
            st.markdown(
                f"**Metadata:** gherkin={_meta.get('gherkin', '—')} · "
                f"acceptance={_meta.get('acceptance_criteria', '—')} · "
                f"other={_meta.get('altro', '—')}"
            )

        st.markdown("---")

        st.markdown("#### 🔀 Pull Request")
        _pr = _issue_data.get("primary_pr") or {}
        if not _pr:
            st.info("No primary PR recorded.")
        else:
            _pc1, _pc2, _pc3 = st.columns(3)
            _pc1.metric("Additions", _pr.get("additions", 0))
            _pc2.metric("Deletions", _pr.get("deletions", 0))
            _pc3.metric("Modified files", _pr.get("changed_files", 0))
            st.markdown(f"**{_pr.get('title', '')}**")
            st.caption(
                f"#{_pr.get('number', '')} · "
                f"branch `{_pr.get('source_branch', '—')}` → `{_pr.get('base_branch', '—')}` · "
                f"merged {_pr.get('merged_at', '—')}"
            )
            _pr_url = _pr.get("url", "")
            if _pr_url:
                st.link_button("🔗 Open PR", _pr_url)

            _merge_info = _issue_data.get("merge_info") or {}
            if _merge_info:
                _mc1, _mc2, _mc3 = st.columns(3)
                _msha = _merge_info.get("merge_commit_sha")
                _mc1.markdown(
                    f"**Merge commit:** `{_msha[:10]}`" if _msha else "**Merge commit:** _N/A_"
                )
                _mc2.markdown(f"**Merge state:** `{_merge_info.get('mergeable_state') or 'N/A'}`")
                _hc = _merge_info.get("had_conflicts")
                if _hc is True:
                    _mc3.markdown("**Conflicts:** ⚠️ yes")
                elif _hc is False:
                    _mc3.markdown("**Conflicts:** ✅ no")
                else:
                    _mc3.markdown("**Conflicts:** ❔ undetermined")

            _timeline = _issue_data.get("timeline") or {}
            if _timeline:
                _delta = _timeline.get("delta_days_issue_closed_to_pr_merged")
                if _delta is None:
                    _delta_str = "_N/A_"
                else:
                    _delta_str = f"**{_delta} days** between issue closing and PR merge"
                    if abs(_delta) > 30:
                        _delta_str += " ⚠️ _(large delta — check consistency)_"
                st.caption(f"⏱️ Timeline: {_delta_str}")

        st.markdown("---")

        st.markdown("#### 📄 Diff")
        _diff = _issue_data.get("diff_between_base_and_last") or []

        # File-list completeness banner; contents are not downloaded, only
        # the SHA and fetch URLs are stored.
        if _issue_data.get("files_capped"):
            st.warning(
                f"File list truncated at the per-PR cap "
                f"({len(_diff)} files kept). The PR touched more files than the cap."
            )
        elif _issue_data.get("files_complete") is False:
            st.warning(
                f"File list incomplete ({len(_diff)} files kept): a GitHub /files "
                "page failed. The captured files are complete and usable."
            )

        if not _diff:
            st.info("No diff recorded.")
        else:
            st.caption(f"**{len(_diff)}** files in the diff between base and last commit.")
            for _f in _diff:
                _fname = _f.get("filename", "?")
                _status = _f.get("status", "?")
                _add = _f.get("additions", 0)
                _dele = _f.get("deletions", 0)
                with st.expander(f"`{_fname}` · **{_status}** · +{_add}/-{_dele}", expanded=False):
                    _prev = _f.get("previous_filename")
                    if _prev:
                        st.caption(f"↪️ renamed from `{_prev}`")
                    _patch = _f.get("patch", "")
                    if _patch:
                        st.code(_patch, language="diff")
                    else:
                        st.caption("_(patch not available — binary or oversized file)_")
                    # Only the fetch URLs are stored, not the contents.
                    _fetch = _f.get("fetch") or {}
                    _links = []
                    if _fetch.get("original_raw"):
                        _links.append(f"[original (base)]({_fetch['original_raw']})")
                    if _fetch.get("modified_raw"):
                        _links.append(f"[modified (head)]({_fetch['modified_raw']})")
                    if _links:
                        st.caption("⬇️ " + " · ".join(_links))