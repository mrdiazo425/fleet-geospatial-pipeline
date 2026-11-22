FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dev tools (pytest, ruff) are included so the same image runs the test suite.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY sql ./sql
COPY src ./src
COPY data ./data
COPY pyproject.toml ./
COPY .streamlit ./.streamlit
COPY tests ./tests

CMD ["python", "-m", "fleet_pipeline.bootstrap"]
