"""Fleet dashboard: sign in, then one page with map, KPIs and charts.

Every filter in the sidebar updates the map, the indicators and the charts.

Run: ``streamlit run src/fleet_pipeline/dashboard/app.py``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium

from fleet_pipeline.config import Settings
from fleet_pipeline.dashboard import charts, data, devices, export, maps, theme
from fleet_pipeline.dashboard.events import alert_events
from fleet_pipeline.privacy import Role, authenticate

LOCAL_TZ = ZoneInfo("America/Bogota")
WINDOWS = {"Últimos 15 min": 15, "Últimos 30 min": 30, "Última hora": 60, "Últimas 2 horas": 120,
           "Últimas 24 horas": 1440}  # fmt: skip
ALERT_LABELS = {
    "speeding": "Exceso de velocidad",
    "harsh_braking": "Frenada brusca",
    "harsh_acceleration": "Aceleración brusca",
    "fuel_insufficient": "Combustible insuficiente",
    "zone_entry": "Ingreso a zona crítica",
}
SEVERITY_LABELS = {"info": "Informativa", "minor": "Leve", "major": "Grave", "severe": "Muy grave"}
ROLE_LABELS = {Role.ADMIN: "Administrador", Role.ANALYST: "Analista"}
INFRACTIONS = ("speeding", "harsh_braking", "harsh_acceleration")
OUTSIDE_ZONES = "Fuera de zonas críticas"
SILENT_LABEL = "Dispositivos sin señal"
CACHE_SECONDS = 10
MAP_HEIGHT = 624  # same height as the three blocks of indicator cards beside it
TOP_ZONES = 6
DRIVER_COLUMNS = {
    "vehicle_id": st.column_config.TextColumn("Vehículo"),
    "driver_score": st.column_config.NumberColumn("Score", format="%.0f"),
    "distance_km": st.column_config.NumberColumn("Km", format="%.1f"),
    "speeding_time_pct": st.column_config.NumberColumn("% en exceso", format="%.1f"),
    "max_speed_kmh": st.column_config.NumberColumn("Vel. máx.", format="%.0f"),
    "harsh_events": st.column_config.NumberColumn("Bruscos"),
    "fuel_alerts": st.column_config.NumberColumn("Comb."),
}
SCORE_HELP = (
    "**Score**: calificación de 0 a 100 de qué tan seguro se condujo. Empieza en 100 y resta "
    "por exceso de velocidad (hasta 50 puntos), maniobras bruscas (hasta 25) y alertas de "
    "combustible (hasta 10). Verde: 80 o más; amarillo: 60 a 79; rojo: menos de 60.\n\n"
    "**Km**: distancia recorrida. **% en exceso**: parte del tiempo de conducción sobre el "
    "límite. **Vel. máx.**: velocidad más alta (km/h). **Bruscos**: frenadas y aceleraciones "
    "bruscas. **Comb.**: veces que el combustible no alcanzaba para terminar la ruta."
)

st.set_page_config(
    page_title="Seguimiento de flota vehicular · Bogotá", page_icon="🚚", layout="wide"
)


# --- data -----------------------------------------------------------------------
@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _load(sources: data.Sources, filters: data.Filters):
    return (
        data.load_points(sources, filters),
        data.load_alerts(sources, filters),
        data.load_driver_kpis(sources, filters),
        data.load_last_seen(sources, filters),
    )


@st.cache_data(ttl=600, show_spinner=False)
def _load_reference(sources: data.Sources):
    return data.load_geofences(sources), data.load_facilities(sources)


@st.cache_data(ttl=60, show_spinner=False)
def _vehicle_ids(sources: data.Sources) -> list[str]:
    return data.load_vehicle_ids(sources)


def label_alerts(alerts: pd.DataFrame, zone_names: dict[str, str]) -> pd.DataFrame:
    """Add the Spanish labels used by charts, insights and the Excel export."""
    return alerts.assign(
        Tipo=alerts["alert_type"].map(ALERT_LABELS),
        Severidad=alerts["severity"].map(SEVERITY_LABELS),
        Zona=alerts["geofence_code"].map(zone_names).fillna(OUTSIDE_ZONES),
    )


def fleet_kpis(
    points: pd.DataFrame, alerts: pd.DataFrame, drivers: pd.DataFrame
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Operation and risk indicators as (label, value, meaning) tuples."""
    km = float(drivers["distance_km"].sum())
    minutes = float(drivers["driving_minutes"].sum())
    speeding_minutes = (
        float((drivers["speeding_time_pct"] * drivers["driving_minutes"]).sum()) / 100
    )
    infractions = int(alerts["alert_type"].isin(INFRACTIONS).sum())
    fuel_alerts = int((alerts["alert_type"] == "fuel_insufficient").sum())
    score = float(drivers["driver_score"].mean()) if len(drivers) else 0.0

    operation = [
        ("Vehículos activos", str(points["vehicle_id"].nunique()), "Reportaron en el periodo"),
        ("Distancia recorrida", f"{theme.number(km)} km", "Suma de toda la flota"),
        ("Tiempo de conducción", theme.duration(minutes), "Con el vehículo en movimiento"),
        (
            "Velocidad media",
            f"{theme.number(km / (minutes / 60), 1) if minutes else '0'} km/h",
            "Distancia / tiempo de conducción",
        ),
    ]
    risk = [
        (
            "Conducción en exceso de velocidad",
            f"{theme.number(100 * speeding_minutes / minutes, 1) if minutes else '0'} %",
            "Del tiempo de conducción, sobre el límite",
        ),
        (
            "Infracciones por cada 100 km",
            theme.number(100 * infractions / km, 1) if km else "0",
            f"{theme.number(infractions)} eventos de infracción en el periodo",
        ),
        ("Alertas de combustible", str(fuel_alerts), "Vehículos sin autonomía para su ruta"),
        ("Driver Score de la flota", f"{score:.0f} / 100", "Promedio; más alto es más seguro"),
    ]
    return operation, risk


def insights(alerts: pd.DataFrame, drivers: pd.DataFrame) -> list[str]:
    """Plain-language findings a manager can act on."""
    findings: list[str] = []
    infractions = alerts[alerts["alert_type"].isin(INFRACTIONS)]
    if not infractions.empty:
        in_zones = infractions[infractions["Zona"] != OUTSIDE_ZONES]
        share = 100 * len(in_zones) / len(infractions)
        findings.append(
            f"<b>{theme.number(share, 1)} %</b> de las infracciones ocurrió dentro de zonas de "
            "alta siniestralidad."
        )
        if not in_zones.empty:
            top = in_zones["Zona"].value_counts()
            findings.append(
                f"Zona más crítica: <b>{top.index[0]}</b> "
                f"({theme.number(top.iloc[0])} infracciones)."
            )
        severe = int((infractions["severity"] == "severe").sum())
        if severe:
            findings.append(
                f"<b>{theme.number(severe)}</b> excesos muy graves "
                "(más de 20 km/h sobre el límite)."
            )
    if len(drivers):
        worst = drivers.sort_values("driver_score").iloc[0]
        at_risk = int((drivers["driver_score"] < 60).sum())
        findings.append(
            f"<b>{at_risk}</b> de {len(drivers)} vehículos tienen puntaje bajo (menos de 60); "
            f"el más bajo es <b>{worst.vehicle_id}</b> con {worst.driver_score:.0f}."
        )
    return findings


def device_kpis(summary: pd.DataFrame, sent: pd.DataFrame) -> list[tuple[str, str, str]]:
    """Device-health indicators as (label, value, meaning) tuples."""
    silent = int((summary["status"] == "Sin señal").sum())
    if sent.empty:
        delay, late_hint = "s/d", "Aún no hay reportes en vivo en el periodo"
    else:
        late = 100.0 * (sent["arrival"] == devices.LATE).mean()
        delay = f"{theme.number(float(sent['arrival_delay_s'].median()), 1)} s"
        late_hint = f"Mediana · {theme.number(late, 1)} % llegó tarde (más de 60 s)"
    return [
        ("Demora de llegada del dato", delay, late_hint),
        (
            SILENT_LABEL,
            f"{silent} de {len(summary)}",
            f"Más de {devices.SILENT_AFTER_MIN} min sin reportar",
        ),
    ]


# --- session --------------------------------------------------------------------
def login(settings: Settings) -> None:
    users = {
        "admin": (settings.admin_api_key, Role.ADMIN),
        "analista": (settings.analyst_ui_password, Role.ANALYST),
    }
    _, centre, _ = st.columns([1, 1.1, 1])
    with centre:
        st.markdown(
            '<h1 class="fleet-title">Seguimiento de flota vehicular <span>· Bogotá</span></h1>',
            unsafe_allow_html=True,
        )
        st.caption("Inicia sesión para ver el tablero.")
        with st.form("login"):
            username = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            submitted = st.form_submit_button("Ingresar", type="primary", use_container_width=True)
        if submitted:
            role = authenticate(username, password, users)
            if role is None:
                st.error("Usuario o contraseña incorrectos.")
            else:
                st.session_state["user"] = username.strip().lower()
                st.session_state["role"] = role
                st.rerun()
        with st.expander("Usuarios de demostración"):
            st.markdown(
                "- **admin** / `admin-secret`: ve los identificadores completos.\n"
                "- **analista** / `analista`: ve los identificadores enmascarados "
                "(`VHC-****-ABC`)."
            )


def sidebar(sources: data.Sources, zone_names: dict[str, str]):
    with st.sidebar:
        st.subheader("Filtros", anchor=False)
        window = st.selectbox("Periodo", list(WINDOWS), index=3)
        vehicles = st.multiselect("Vehículos", _vehicle_ids(sources), placeholder="Todos")
        zones = st.multiselect(
            "Zona de alta siniestralidad",
            list(zone_names),
            format_func=zone_names.get,
            placeholder="Toda la ciudad",
        )
        alert_types = st.multiselect(
            "Infracciones en el mapa de calor",
            list(INFRACTIONS),
            default=list(INFRACTIONS),
            format_func=ALERT_LABELS.get,
        )
        st.divider()
        st.subheader("Vista", anchor=False)
        live = st.toggle("Actualizar cada 15 segundos", value=False)
        show_facilities = st.toggle("Mostrar centros de salud y educativos", value=False)

    since = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=WINDOWS[window])
    filters = data.Filters(since, tuple(vehicles) or None, tuple(zones) or None)
    return window, filters, alert_types, show_facilities, live


def user_menu(role: Role, kpis, drivers, alerts, device_table) -> None:
    """Top-right menu: who is signed in, export, print and sign out."""
    with st.popover(f"👤 {ROLE_LABELS[role]}", use_container_width=True):
        st.caption(f"Sesión iniciada como **{st.session_state['user']}**")
        if kpis:
            stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M")
            st.download_button(
                "⬇ Descargar Excel",
                export.to_excel(kpis, drivers, alerts, device_table),
                file_name=f"seguimiento_flota_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        components.html(
            '<button onclick="window.parent.print()" style="width:100%;padding:.45rem;'
            "border-radius:8px;border:1px solid #3a4a66;background:#121c2e;color:#e8eef7;"
            'font-family:sans-serif;font-size:.95rem;cursor:pointer">🖨 Imprimir / PDF</button>',
            height=44,
        )
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.clear()
            st.rerun()


# --- page -----------------------------------------------------------------------
def render(role, sources, window, filters, alert_types, show_facilities, zone_names) -> None:
    points, raw_alerts, drivers, last_seen = _load(sources, filters)
    geofences, facilities = _load_reference(sources)
    # Counts and charts use events; the heatmap keeps every report (exposure).
    alerts = label_alerts(alert_events(raw_alerts), zone_names)
    device_table = devices.device_summary(points, last_seen, datetime.now(UTC))
    sent = devices.transmitted(points)
    operation, risk = fleet_kpis(points, alerts, drivers) if not points.empty else ([], [])
    device = device_kpis(device_table, sent)

    updated = datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M:%S")
    title_column, menu_column = st.columns([6, 1])
    title_column.markdown(
        '<h1 class="fleet-title">Seguimiento de flota vehicular <span>· Bogotá</span></h1>'
        '<div class="fleet-meta">'
        f'<span class="fleet-chip">Actualizado: <b>{updated}</b></span>'
        f'<span class="fleet-chip">Periodo: <b>{window}</b></span>'
        f'<span class="fleet-chip">Rol: <b>{ROLE_LABELS[role]}</b></span></div>',
        unsafe_allow_html=True,
    )
    with menu_column:
        user_menu(role, operation + risk + device, drivers, alerts, device_table)
    if points.empty:
        st.warning("No hay reportes para los filtros seleccionados. Amplía el periodo.")
        return

    findings = insights(alerts, drivers)
    if findings:
        st.markdown(
            '<div class="fleet-insights"><div class="fleet-section">Hallazgos clave</div>'
            + " &nbsp;·&nbsp; ".join(findings)
            + "</div>",
            unsafe_allow_html=True,
        )

    infractions = alerts[alerts["alert_type"].isin(INFRACTIONS)]
    minutes = WINDOWS[window]
    bin_minutes = 1 if minutes <= 30 else 5 if minutes <= 120 else 30

    # Row 1: map + indicator cards
    map_column, cards_column = st.columns([3, 2], gap="medium")
    with map_column:
        heat = raw_alerts[raw_alerts["alert_type"].isin(alert_types)]
        fmap = maps.build_map(points, heat, geofences, facilities if show_facilities else None)
        st_folium(fmap, height=MAP_HEIGHT, use_container_width=True, returned_objects=[])
    silent = any(value != "0" and label == SILENT_LABEL for label, value, _ in _counts(device))
    cards_column.markdown(
        '<div class="fleet-section">Operación</div>'
        + theme.cards(operation)
        + '<div class="fleet-section">Riesgo y seguridad</div>'
        + theme.cards(risk)
        + '<div class="fleet-section">Dispositivo</div>'
        + theme.cards(device, frozenset({SILENT_LABEL}) if silent else frozenset()),
        unsafe_allow_html=True,
    )

    # Row 2: alert breakdowns + driver score
    charts_column, score_column = st.columns([2, 3], gap="medium")
    with charts_column:
        if alerts.empty:
            st.info("No hay alertas con los filtros seleccionados.")
        else:
            st.altair_chart(
                charts.horizontal_bars(alerts, "Tipo", "Alertas por tipo y severidad"),
                use_container_width=True,
            )
            if not infractions.empty:
                st.altair_chart(
                    charts.horizontal_bars(
                        infractions, "Zona", "Infracciones por zona (principales)", top=TOP_ZONES
                    ),
                    use_container_width=True,
                )
    with score_column:
        st.subheader("Driver Score por vehículo", help=SCORE_HELP, anchor=False)
        st.dataframe(
            theme.style_drivers(drivers),
            hide_index=True,
            use_container_width=True,
            height=min(38 + 35 * len(drivers), 458),  # grows with the fleet, then scrolls
            column_order=list(DRIVER_COLUMNS),
            column_config=DRIVER_COLUMNS,
        )

    # Row 3: infractions and device response over time
    time_column, delay_column = st.columns(2, gap="medium")
    with time_column:
        if infractions.empty:
            st.info("Sin infracciones en el periodo.")
        else:
            st.altair_chart(
                charts.timeline(infractions, bin_minutes, "Infracciones en el tiempo"),
                use_container_width=True,
            )
    with delay_column:
        if sent.empty:
            st.info("Aún no hay reportes en vivo en el periodo (solo carga histórica).")
        else:
            st.altair_chart(
                charts.arrival_delay(sent, bin_minutes, "Tiempo de respuesta del dispositivo"),
                use_container_width=True,
            )


def _counts(cards: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Cards with the value reduced to its leading number ('2 de 12' -> '2')."""
    return [(label, value.split(" ")[0], hint) for label, value, hint in cards]


def main() -> None:
    settings = Settings.from_env()
    st.markdown(theme.css(theme.DARK), unsafe_allow_html=True)

    role = st.session_state.get("role")
    if role is None:
        login(settings)
        return
    try:
        sources = data.sources_for(role, settings)
        geofences, _ = _load_reference(sources)
        zone_names = {
            row.zone_code: f"{row.zone_code} · {row.locality}"
            for row in geofences.itertuples()
            if row.geofence_type == "high_accident"
        }
        window, filters, alert_types, show_facilities, live = sidebar(sources, zone_names)
        body = st.fragment(run_every=15 if live else None)(render)
        body(role, sources, window, filters, alert_types, show_facilities, zone_names)
    except Exception as error:  # show a readable message instead of a stack trace
        st.error(f"No fue posible consultar la base de datos: {error}")


main()
