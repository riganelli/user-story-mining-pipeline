"""
UI helpers — CSS injection and reusable components.
"""
from pathlib import Path
import streamlit as st


_CSS_PATH = Path(__file__).parent.parent / "static" / "styles.css"


def inject_css() -> None:
    """Inject custom CSS into every page. Call at the top of each page file."""
    if _CSS_PATH.exists():
        css = _CSS_PATH.read_text(encoding="utf-8")
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


def hero(title: str, subtitle: str = "") -> None:
    """Main title (home page) with large animated gradient."""
    st.markdown(
        f"""
        <div>
            <h1 class="hero-title">{title}</h1>
            <p class="hero-subtitle">{subtitle}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def page_hero(title: str, subtitle: str = "") -> None:
    """Inner page title with a more compact animated gradient."""
    st.markdown(
        f"""
        <div class="page-hero-wrap">
            <h1 class="page-hero-title">{title}</h1>
            <p class="page-hero-subtitle">{subtitle}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def badge(text: str, color: str = "violet") -> str:
    """Return the HTML of a colored badge. Use inside st.markdown with unsafe_allow_html=True."""
    cls = "badge" if color == "violet" else f"badge badge-{color}"
    return f'<span class="{cls}">{text}</span>'


# Consistent palette for the Plotly charts.
PLOTLY_COLORS = {
    "violet": "#8B5CF6",
    "cyan": "#06B6D4",
    "pink": "#EC4899",
    "amber": "#F59E0B",
    "green": "#10B981",
    "muted": "#6B6E7A",
}

PLOTLY_SEQUENCE = [
    "#8B5CF6", "#06B6D4", "#EC4899",
    "#F59E0B", "#10B981", "#A0A3AE",
]


def plotly_layout(**overrides):
    """Base Plotly layout consistent with the dark theme."""
    base = dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, sans-serif", color="#E4E6EB", size=13),
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)", zerolinecolor="rgba(255,255,255,0.1)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)", zerolinecolor="rgba(255,255,255,0.1)"),
        margin=dict(l=40, r=20, t=40, b=40),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
    )
    base.update(overrides)
    return base