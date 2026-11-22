# SETUP: despliegue local paso a paso

Toda la solución se levanta con un comando. No hay que instalar Python, PostgreSQL ni
PostGIS en el equipo: todo corre en contenedores.

## 1. Requisitos

| Requisito | Versión | Cómo verificar |
|---|---|---|
| Docker Desktop (Windows/macOS) o Docker Engine (Linux) | 24 o superior | `docker --version` |
| Docker Compose | v2 (incluido en Docker Desktop) | `docker compose version` |
| Memoria libre | 2 GB | — |
| Puertos libres | 5433, 8000 y 8501 | Si alguno está ocupado, ver la sección 7 |

En Windows, Docker Desktop debe estar abierto antes de continuar.

## 2. Obtener el código

```bash
git clone https://github.com/mrdiazo425/fleet-geospatial-pipeline.git
cd fleet-geospatial-pipeline
```

## 3. Levantar la solución

```bash
docker compose up -d --build
```

La primera vez tarda unos minutos porque descarga las imágenes de PostGIS y Python. Este
comando hace, en orden:

1. **`db`**: inicia PostgreSQL 16 con PostGIS 3.4.
2. **`bootstrap`** (se ejecuta una vez y termina): inicializa la base de datos y carga los
   datos de referencia.
3. **`api`**: inicia la API de ingesta.
4. **`simulator`**: carga dos horas de historia y luego reporta en vivo cada 5 segundos.
5. **`pipeline`**: procesa Bronce → Plata → Oro cada 10 segundos.
6. **`dashboard`**: inicia el tablero.

### Inicialización de la base de datos y extensiones espaciales

No requiere pasos manuales. El servicio `bootstrap` aplica, en orden, los archivos de `sql/`:

| Archivo | Qué crea |
|---|---|
| `001_extensions.sql` | `CREATE EXTENSION postgis` y el registro del sistema de referencia EPSG:9377 (MAGNA-SIRGAS 2018 / Origen Nacional) |
| `002_schemas.sql` | Esquemas `reference`, `bronze`, `silver` y `gold` |
| `010_reference.sql`, `011_fleet.sql` | Geocercas, vías, equipamientos, vehículos y rutas, con índices GiST |
| `020_bronze.sql` | Telemetría cruda, función de enmascaramiento y rol `fleet_analyst` |
| `030_silver.sql`, `031_silver_pipeline.sql` | Tablas de Plata, reglas de negocio y proceso por lotes |
| `040_gold.sql` | Tabla consolidada, Driver Score y vistas para BI |

Después carga `data/reference.gpkg` y `data/fleet.gpkg` en PostGIS. Todos los archivos son
idempotentes: se pueden volver a aplicar sin dañar datos.

## 4. Verificar que todo funciona

**Estado de los servicios**

```bash
docker compose ps -a
```

Resultado esperado: `db` y `api` en `healthy`; `simulator`, `pipeline` y `dashboard` en `Up`;
`bootstrap` en `Exited (0)` (es correcto: corre una vez y termina).

**Carga de datos de referencia**

```bash
docker compose logs bootstrap
```

Debe terminar con:

```
Reference data loaded: {'geofences': 16, 'roads': 51447, 'facilities': 2092, 'geofence_parts': 271, 'vehicles': 12, 'vehicle_routes': 12}
Bootstrap finished OK
```

**Extensión espacial**

```bash
docker compose exec db psql -U fleet -d fleet -c "SELECT postgis_version()"
```

**Pipeline procesando**

```bash
docker compose logs pipeline
```

Primero un ciclo grande con la historia (unos 13.600 reportes) y luego ciclos pequeños cada
10 segundos.

**Consulta espacial de geocerca** (debe responder `PUR001`, el perímetro urbano):

```bash
docker compose exec db psql -U fleet -d fleet -c "SELECT g.zone_code FROM silver.geofences_at(ST_Transform(ST_SetSRID(ST_MakePoint(-74.0721, 4.7110), 4326), 9377)) f JOIN reference.geofences g USING (geofence_id)"
```

## 5. Usar la solución

### Tablero

Abrir http://localhost:8501 e ingresar con uno de los usuarios de demostración:

| Usuario | Contraseña | Rol | Identificadores |
|---|---|---|---|
| `admin` | `admin-secret` | Administrador | Completos (`VHC-8472-ABC`) |
| `analista` | `analista` | Analista | Enmascarados (`VHC-****-ABC`) |

El panel lateral filtra por periodo, vehículo y zona; el menú del rol (arriba a la derecha)
permite descargar el Excel, imprimir y cerrar sesión.

### API

Documentación interactiva en http://localhost:8000/docs.

```bash
# Sin clave: identificadores enmascarados
curl "http://localhost:8000/telemetry?limit=3"

# Con clave de administrador: identificadores completos
curl -H "X-API-Key: admin-secret" "http://localhost:8000/telemetry?limit=3"

# Enviar un paquete
curl -X POST http://localhost:8000/telemetry -H "Content-Type: application/json" \
  -d '{"vehicle_id":"VHC-8472-ABC","timestamp":"2026-11-22T08:00:00-05:00","latitude":4.711,"longitude":-74.0721,"speed_kmh":42.5,"fuel_level_pct":63.2}'
```

En PowerShell use `curl.exe` en lugar de `curl`.

### Base de datos (psql, QGIS, DBeaver, Power BI)

| Parámetro | Valor |
|---|---|
| Host / puerto | `localhost` / `5433` |
| Base de datos | `fleet` |
| Administrador | `fleet` / `fleet` |
| Analista (solo vistas enmascaradas) | `fleet_analyst` / `analyst` |

Tablas y vistas más útiles: `gold.fact_telemetry`, `gold.driver_score_daily`, `gold.alerts`,
`gold.infraction_hotspots`, `gold.road_speed_profile` y `reference.geofences`.

Comprobación del enmascaramiento a nivel de base de datos:

```bash
# Devuelve un identificador enmascarado
docker compose exec db psql -U fleet_analyst -d fleet -c "SELECT vehicle_id FROM gold.v_fact_telemetry LIMIT 1"

# Falla con "permission denied": el analista no puede leer la tabla cruda
docker compose exec db psql -U fleet_analyst -d fleet -c "SELECT vehicle_id FROM bronze.telemetry_raw LIMIT 1"
```

## 6. Pruebas y rendimiento

```bash
# Pruebas unitarias (173). Las de geocercas y combustible corren contra PostGIS,
# en una base aparte (fleet_test) que se crea sola.
docker compose run --rm tests

# Efecto de los índices GiST sobre 1 millón de puntos (tarda cerca de un minuto)
docker compose run --rm pipeline python -m fleet_pipeline.benchmark
```

## 7. Configuración

La solución funciona sin configurar nada. Para cambiar puertos, contraseñas o parámetros del
simulador, copie `.env.example` a `.env` y edite los valores:

```bash
cp .env.example .env
```

| Variable | Valor por defecto | Para qué |
|---|---|---|
| `DB_HOST_PORT` | `5433` | Puerto de PostGIS en el equipo (5433 evita chocar con un PostgreSQL local) |
| `API_HOST_PORT` | `8000` | Puerto de la API |
| `DASHBOARD_HOST_PORT` | `8501` | Puerto del tablero |
| `ADMIN_API_KEY` | `admin-secret` | Clave de administrador de la API y contraseña del usuario `admin` |
| `ANALYST_UI_PASSWORD` | `analista` | Contraseña del usuario `analista` |
| `SIM_INTERVAL_S` | `5` | Segundos entre reportes de cada vehículo |
| `SIM_BACKFILL_MINUTES` | `120` | Minutos de historia que se cargan al arrancar |
| `PIPELINE_INTERVAL_S` | `10` | Segundos entre ciclos del pipeline |

Las credenciales por defecto son de demostración y no deben usarse fuera de un entorno local.

## 8. Operación

| Acción | Comando |
|---|---|
| Ver registros de un servicio | `docker compose logs -f pipeline` |
| Detener (conserva los datos) | `docker compose down` |
| Detener y borrar la base | `docker compose down -v` |
| Reconstruir tras cambiar código | `docker compose up -d --build` |
| Empezar de cero (base nueva y datos recargados) | `docker compose down -v` y luego `docker compose up -d --build` |

## 9. Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `port is already allocated` | Otro programa usa el puerto | Cambiar el puerto en `.env` (sección 7) |
| `bootstrap` termina con código distinto de 0 | Falló la carga inicial | `docker compose logs bootstrap` muestra el motivo |
| El tablero dice "No hay reportes" | El simulador aún no termina la carga inicial | Esperar un minuto y revisar `docker compose logs simulator` |
| La tarjeta "Demora de llegada" dice "s/d" | Solo hay carga histórica todavía | Esperar a que lleguen reportes en vivo |
| `psql` parece bloqueado mostrando una tabla | Se abrió el paginador | Salir con `q`, o agregar `-P pager=off` |
| Cambios de código que no se reflejan | La imagen no se reconstruyó | `docker compose up -d --build` |

## 10. Desarrollo sin Docker (opcional)

Requiere Python 3.12 y un PostgreSQL con PostGIS accesible.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export PYTHONPATH=src DB_HOST=localhost DB_PORT=5433  # PowerShell: $env:PYTHONPATH="src"

python -m fleet_pipeline.bootstrap                    # esquema + datos de referencia
uvicorn fleet_pipeline.api.app:app --port 8000        # API
python -m fleet_pipeline.simulator.runner             # simulador
python -m fleet_pipeline.pipeline.runner              # pipeline
streamlit run src/fleet_pipeline/dashboard/app.py     # tablero

ruff check . && ruff format --check . && pytest       # calidad y pruebas
```
