"""Visual identity of the dashboard: palettes, CSS and small HTML builders."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

import pandas as pd


@dataclass(frozen=True)
class Palette:
    base: str
    background: str
    surface: str
    border: str
    text: str
    muted: str
    accent: str


# Mirrors .streamlit/config.toml, for the custom HTML pieces.
DARK = Palette("dark", "#0b1220", "#121c2e", "#22314b", "#e8eef7", "#8b9bb4", "#19d3ae")

GOOD, WARNING, BAD = "#2fbf71", "#f2a541", "#e5484d"
SEVERITY_ORDER = ["Informativa", "Leve", "Grave", "Muy grave"]
SEVERITY_COLORS = ["#8d99ae", "#4ea8de", "#ef6351", "#b3001b"]


def score_color(score: float) -> str:
    """Traffic light for a 0-100 Driver Score."""
    if score >= 80:
        return GOOD
    return WARNING if score >= 60 else BAD


def number(value: float, decimals: int = 0) -> str:
    """Format with Spanish separators: 1.234,5"""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def duration(minutes: float) -> str:
    hours, rest = divmod(int(round(minutes)), 60)
    return f"{hours} h {rest:02d} min" if hours else f"{rest} min"


def css(p: Palette) -> str:
    return f"""
    <style>
      .block-container {{ padding-top: 1.6rem; padding-bottom: 2rem; max-width: 1700px; }}
      [data-testid="stHeader"] {{ background: transparent; height: 0; }}
      h1.fleet-title {{ font-size: 1.9rem; font-weight: 700; margin: 0; color: {p.text}; }}
      h1.fleet-title span {{ color: {p.accent}; }}
      .fleet-meta {{ display: flex; gap: .6rem; align-items: center; flex-wrap: wrap;
                     margin: .35rem 0 1rem 0; }}
      .fleet-chip {{ border: 1px solid {p.border}; background: {p.surface}; color: {p.text};
                     border-radius: 999px; padding: .2rem .8rem; font-size: .9rem; }}
      .fleet-chip b {{ color: {p.accent}; }}
      .fleet-section {{ color: {p.muted}; text-transform: uppercase; letter-spacing: .08em;
                        font-size: .72rem; font-weight: 700; margin: .2rem 0 .4rem 0; }}
      .fleet-cards {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: .7rem;
                      margin-bottom: .9rem; }}
      .fleet-card {{ background: {p.surface}; border: 1px solid {p.border};
                     border-left: 4px solid {p.accent}; border-radius: 12px;
                     padding: .8rem 1rem; }}
      .fleet-card.alert {{ border-left-color: {BAD}; }}
      .fleet-card .label {{ color: {p.muted}; font-size: .82rem; }}
      .fleet-card .value {{ color: {p.text}; font-size: 1.55rem; font-weight: 700;
                            line-height: 1.25; }}
      .fleet-card .hint {{ color: {p.muted}; font-size: .74rem; }}
      .fleet-insights {{ background: {p.surface}; border: 1px solid {p.border};
                         border-radius: 12px; padding: .7rem 1rem; margin-bottom: 1rem;
                         color: {p.text}; font-size: .92rem; }}
      .fleet-insights b {{ color: {p.accent}; }}
      /* The table toolbar offers CSV only; the Excel export lives in the user menu. */
      [data-testid="stElementToolbar"]
        [data-testid="stElementToolbarButton"]:first-child:nth-last-child(3) {{ display: none; }}
      @media print {{
        @page {{ size: A3 landscape; margin: 8mm; }}
        * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
        [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
        [data-testid="stToolbar"], [data-testid="stHeader"], [data-testid="stPopover"],
        [data-baseweb="popover"], [data-testid="stElementToolbar"],
        [data-testid="stExpander"], [data-testid="stStatusWidget"] {{ display: none !important; }}
        html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
        [data-testid="stMainBlockContainer"], section.main {{
          height: auto !important; min-height: 0 !important; overflow: visible !important;
          position: static !important; }}
        .block-container {{ max-width: 100% !important; padding: 0 !important; }}
        .fleet-card, .fleet-insights, [data-testid="stDataFrame"], [data-testid="stVegaLiteChart"],
        iframe {{ break-inside: avoid; }}
      }}
    </style>
    """


def cards(items: list[tuple[str, str, str]], alert_labels: frozenset[str] = frozenset()) -> str:
    """Grid of KPI cards from (label, value, hint) tuples.

    Cards whose label is in ``alert_labels`` get a red edge to draw the eye.
    """
    body = "".join(
        f'<div class="fleet-card{" alert" if label in alert_labels else ""}">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div><div class="hint">{escape(hint)}</div></div>'
        for label, value, hint in items
    )
    return f'<div class="fleet-cards">{body}</div>'


def style_drivers(drivers: pd.DataFrame):
    """Colour the score cell like a traffic light (keeps the table sortable)."""

    def score_cell(score: float) -> str:
        return f"background-color: {score_color(score)}; color: #ffffff; font-weight: 700"

    return drivers.style.map(score_cell, subset=["driver_score"])
