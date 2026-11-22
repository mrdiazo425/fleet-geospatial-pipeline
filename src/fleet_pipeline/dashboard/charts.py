"""Altair charts. Severity always uses the same colours across the page."""

from __future__ import annotations

import altair as alt
import pandas as pd

from fleet_pipeline.dashboard.theme import SEVERITY_COLORS, SEVERITY_ORDER

_SEVERITY = alt.Color(
    "Severidad:N",
    scale=alt.Scale(domain=SEVERITY_ORDER, range=SEVERITY_COLORS),
    legend=alt.Legend(orient="bottom", title=None, labelLimit=200),
)
_ORDER = alt.Order("severity_rank:Q")


def _ranked(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(severity_rank=frame["Severidad"].map(SEVERITY_ORDER.index))


TITLE_FONT_SIZE = 24  # same as the page's section headings
LABEL_FONT_SIZE = 13
BAR_STEP = 26  # pixels per category: every label stays readable


def _title(text: str) -> alt.TitleParams:
    """Title inside the chart, so it is still there in full-screen view."""
    return alt.TitleParams(
        text, anchor="start", fontSize=TITLE_FONT_SIZE, fontWeight=600, offset=14
    )


def _styled(chart: alt.Chart) -> alt.Chart:
    return chart.configure_axis(
        labelFontSize=LABEL_FONT_SIZE, titleFontSize=LABEL_FONT_SIZE
    ).configure_legend(labelFontSize=LABEL_FONT_SIZE, symbolSize=130)


def horizontal_bars(
    frame: pd.DataFrame, category: str, title: str, top: int | None = None
) -> alt.Chart:
    """Stacked horizontal bars: one bar per ``category``, split by severity.

    ``top`` keeps only the categories with most alerts. The height follows the
    number of bars, so no category is ever squeezed out.
    """
    if top is not None:
        keep = frame[category].value_counts().head(top).index
        frame = frame[frame[category].isin(keep)]
    counts = _ranked(frame).groupby([category, "Severidad", "severity_rank"]).size()
    data = counts.rename("Alertas").reset_index()
    chart = (
        alt.Chart(data)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X("Alertas:Q", title=None),
            y=alt.Y(f"{category}:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            color=_SEVERITY,
            order=_ORDER,
            tooltip=[category, "Severidad", "Alertas"],
        )
        .properties(height=alt.Step(BAR_STEP), title=_title(title))
    )
    return _styled(chart)


def timeline(frame: pd.DataFrame, bin_minutes: int, title: str, height: int = 300) -> alt.Chart:
    """Infractions over time (local time), stacked by severity."""
    local = frame["reported_at"].dt.tz_convert("America/Bogota").dt.tz_localize(None)
    binned = _ranked(frame).assign(Hora=local.dt.floor(f"{bin_minutes}min"))
    counts = binned.groupby(["Hora", "Severidad", "severity_rank"]).size()
    data = counts.rename("Infracciones").reset_index()
    chart = (
        alt.Chart(data)
        .mark_bar()
        .encode(
            x=alt.X("Hora:T", title=None, axis=alt.Axis(format="%H:%M")),
            y=alt.Y("Infracciones:Q", title=None),
            color=_SEVERITY,
            order=_ORDER,
            tooltip=[alt.Tooltip("Hora:T", format="%H:%M"), "Severidad", "Infracciones"],
        )
        .properties(height=height, title=_title(title))
    )
    return _styled(chart)


def arrival_delay(
    frame: pd.DataFrame, bin_minutes: int, title: str, height: int = 300
) -> alt.Chart:
    """Typical and worst arrival delay of the reports received in each interval."""
    local = frame["reported_at"].dt.tz_convert("America/Bogota").dt.tz_localize(None)
    binned = frame.assign(Hora=local.dt.floor(f"{bin_minutes}min")).groupby("Hora")
    delays = binned["arrival_delay_s"].agg(Mediana="median", Máxima="max").reset_index()
    data = delays.melt("Hora", var_name="Demora", value_name="Segundos")
    chart = (
        alt.Chart(data)
        .mark_line(point=True, strokeWidth=2.5)
        .encode(
            x=alt.X("Hora:T", title=None, axis=alt.Axis(format="%H:%M")),
            y=alt.Y("Segundos:Q", title="Segundos"),
            color=alt.Color(
                "Demora:N",
                scale=alt.Scale(domain=["Mediana", "Máxima"], range=["#19d3ae", "#ef6351"]),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=[
                alt.Tooltip("Hora:T", format="%H:%M"),
                "Demora",
                alt.Tooltip("Segundos:Q", format=".1f"),
            ],
        )
        .properties(height=height, title=_title(title))
    )
    return _styled(chart)
