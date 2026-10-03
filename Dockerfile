# The one deployment image (TECHNICAL_DESIGN.md §5): `api` (default command), `worker`
# (`python -m eca.worker`) and `release` (`alembic upgrade head`). ffmpeg/ffprobe for the media
# stages come from the base distribution (§16.1). Tests, evaluation data, the web app and local
# files are not copied (see .dockerignore). Configuration and secrets come from the environment.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY config/ config/
COPY backend/pyproject.toml backend/README.md backend/alembic.ini backend/
COPY backend/migrations/ backend/migrations/
COPY backend/eca/ backend/eca/
# Editable install keeps the repository layout (config/ next to backend/), which the settings use
# to find config/ by default.
RUN pip install -e ./backend \
    && useradd --create-home --uid 10001 eca

USER eca
WORKDIR /app/backend
EXPOSE 8000
CMD ["uvicorn", "eca.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
