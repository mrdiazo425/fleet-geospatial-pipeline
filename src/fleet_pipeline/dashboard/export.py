"""Excel export of everything shown on the page (already role-filtered)."""

from __future__ import annotations

from io import BytesIO

import pandas as pd

DRIVER_COLUMNS = {
    "vehicle_id": "Vehículo",
    "driver_score": "Driver Score (0-100)",
    "distance_km": "Distancia (km)",
    "driving_minutes": "Tiempo de conducción (min)",
    "speeding_time_pct": "% del tiempo en exceso de velocidad",
    "max_speed_kmh": "Velocidad máxima (km/h)",
    "zone_minutes": "Minutos en zona de alta siniestralidad",
    "harsh_events": "Frenadas y aceleraciones bruscas",
    "fuel_alerts": "Alertas de combustible",
}
DEVICE_COLUMNS = {
    "vehicle_id": "Vehículo",
    "status": "Estado",
    "last_report": "Último reporte (Bogotá)",
    "minutes_silent": "Minutos sin reportar",
    "median_delay_s": "Demora de llegada mediana (s)",
    "max_delay_s": "Demora de llegada máxima (s)",
    "late_pct": "% de reportes tardíos",
    "reports": "Reportes transmitidos en el periodo",
}
NOTES = [
    ("Horas", "Todas las fechas y horas están en hora de Bogotá (UTC-5)."),
    (
        "Fecha y hora de toma",
        "Momento en que el dispositivo GPS del vehículo registró el dato.",
    ),
    (
        "Fecha y hora de llegada",
        "Momento en que la plataforma recibió y almacenó el dato (capa Bronce).",
    ),
    (
        "Demora de llegada",
        "Segundos entre la toma y la llegada. Queda vacía en los datos de carga histórica "
        "(el historial que el simulador envía al iniciar), porque esa diferencia no es una "
        "demora de transmisión.",
    ),
    (
        "Dispositivos",
        "Estado 'Sin señal': el dispositivo lleva más de 15 minutos sin reportar. Un reporte "
        "es tardío si tardó más de 60 segundos en llegar. La carga histórica no se cuenta.",
    ),
    (
        "Alertas",
        "Cada fila es un evento: los reportes consecutivos del mismo tipo y vehículo se "
        "agrupan. La toma y la llegada corresponden al primer reporte del evento.",
    ),
]
ALERT_COLUMNS = {
    "vehicle_id": "Vehículo",
    "Toma": "Fecha y hora de toma (dispositivo)",
    "Llegada": "Fecha y hora de llegada (sistema)",
    "Demora (s)": "Demora de llegada (s)",
    "Tipo": "Tipo",
    "Severidad": "Severidad",
    "Zona": "Zona",
    "observed": "Valor máximo observado",
    "threshold": "Umbral",
    "duration_s": "Duración (s)",
    "reports": "Reportes GPS",
    "latitude": "Latitud",
    "longitude": "Longitud",
}


def _local(timestamps: pd.Series) -> pd.Series:
    """Bogotá wall-clock time without tz info (Excel cannot store time zones)."""
    converted = pd.to_datetime(timestamps, utc=True).dt.tz_convert("America/Bogota")
    return converted.dt.tz_localize(None)


def to_excel(
    kpis: list[tuple[str, str, str]],
    drivers: pd.DataFrame,
    alerts: pd.DataFrame,
    devices: pd.DataFrame,
) -> bytes:
    """Workbook: indicators, driver score, devices, alerts by zone, alerts and notes."""
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(kpis, columns=["Indicador", "Valor", "Qué significa"]).to_excel(
            writer, sheet_name="Indicadores", index=False
        )
        drivers[list(DRIVER_COLUMNS)].rename(columns=DRIVER_COLUMNS).to_excel(
            writer, sheet_name="Driver Score", index=False
        )
        if not devices.empty:
            device_sheet = devices.assign(last_report=_local(devices["last_report"]))
            device_sheet[list(DEVICE_COLUMNS)].rename(columns=DEVICE_COLUMNS).round(1).to_excel(
                writer, sheet_name="Dispositivos", index=False
            )
        if not alerts.empty:
            by_zone = alerts.groupby(["Zona", "Tipo", "Severidad"]).size().rename("Alertas")
            by_zone.reset_index().to_excel(writer, sheet_name="Alertas por zona", index=False)
            taken, arrived = _local(alerts["reported_at"]), _local(alerts["ingested_at"])
            detail = alerts.assign(
                Toma=taken,
                Llegada=arrived,
                **{"Demora (s)": alerts["arrival_delay_s"].round(1)},
            )
            detail[list(ALERT_COLUMNS)].rename(columns=ALERT_COLUMNS).to_excel(
                writer, sheet_name="Alertas", index=False
            )
        pd.DataFrame(NOTES, columns=["Tema", "Nota"]).to_excel(
            writer, sheet_name="Notas", index=False
        )
        for sheet in writer.sheets.values():
            for column in sheet.columns:
                width = max(len(str(cell.value or "")) for cell in column) + 2
                sheet.column_dimensions[column[0].column_letter].width = min(width, 60)
    return buffer.getvalue()
