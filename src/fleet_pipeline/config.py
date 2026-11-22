"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: CRS of the reference layers and of every metric operation (metres).
METRIC_SRID = 9377
#: CRS reported by GPS devices and expected by web maps.
GPS_SRID = 4326


@dataclass(frozen=True)
class Settings:
    """Application settings. Defaults match ``docker-compose.yml``."""

    db_host: str = "localhost"
    db_port: int = 5433
    db_name: str = "fleet"
    db_user: str = "fleet"
    db_password: str = "fleet"
    analyst_db_user: str = "fleet_analyst"
    analyst_db_password: str = "analyst"
    sql_dir: Path = PROJECT_ROOT / "sql"
    reference_gpkg: Path = PROJECT_ROOT / "data" / "reference.gpkg"
    fleet_gpkg: Path = PROJECT_ROOT / "data" / "fleet.gpkg"
    urban_speed_limit_kmh: int = 50
    api_url: str = "http://localhost:8000"
    admin_api_key: str = "admin-secret"
    analyst_ui_password: str = "analista"
    sim_interval_s: float = 5.0
    sim_backfill_minutes: int = 120
    sim_seed: int = 42
    pipeline_interval_s: float = 10.0
    pipeline_batch_size: int = 20000

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the environment, falling back to defaults."""
        defaults = cls()
        return cls(
            db_host=os.getenv("DB_HOST", defaults.db_host),
            db_port=int(os.getenv("DB_PORT", defaults.db_port)),
            db_name=os.getenv("DB_NAME", defaults.db_name),
            db_user=os.getenv("DB_USER", defaults.db_user),
            db_password=os.getenv("DB_PASSWORD", defaults.db_password),
            analyst_db_user=os.getenv("ANALYST_DB_USER", defaults.analyst_db_user),
            analyst_db_password=os.getenv("ANALYST_DB_PASSWORD", defaults.analyst_db_password),
            sql_dir=Path(os.getenv("SQL_DIR", defaults.sql_dir)),
            reference_gpkg=Path(os.getenv("REFERENCE_GPKG", defaults.reference_gpkg)),
            fleet_gpkg=Path(os.getenv("FLEET_GPKG", defaults.fleet_gpkg)),
            urban_speed_limit_kmh=int(
                os.getenv("URBAN_SPEED_LIMIT_KMH", defaults.urban_speed_limit_kmh)
            ),
            api_url=os.getenv("API_URL", defaults.api_url).rstrip("/"),
            admin_api_key=os.getenv("ADMIN_API_KEY", defaults.admin_api_key),
            analyst_ui_password=os.getenv("ANALYST_UI_PASSWORD", defaults.analyst_ui_password),
            sim_interval_s=float(os.getenv("SIM_INTERVAL_S", defaults.sim_interval_s)),
            sim_backfill_minutes=int(
                os.getenv("SIM_BACKFILL_MINUTES", defaults.sim_backfill_minutes)
            ),
            sim_seed=int(os.getenv("SIM_SEED", defaults.sim_seed)),
            pipeline_interval_s=float(
                os.getenv("PIPELINE_INTERVAL_S", defaults.pipeline_interval_s)
            ),
            pipeline_batch_size=int(os.getenv("PIPELINE_BATCH_SIZE", defaults.pipeline_batch_size)),
        )

    @property
    def analyst_conninfo(self) -> str:
        """Connection string for the non-admin role (masked views only)."""
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.analyst_db_user} password={self.analyst_db_password}"
        )

    @property
    def conninfo(self) -> str:
        """libpq connection string."""
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password}"
        )
