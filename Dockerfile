FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    PATH="/app/.venv/bin:$PATH"

# ffmpeg: merge/convert · nodejs: JS runtime yt-dlp needs for YouTube
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .
EXPOSE 8000
# The project venv is already on PATH — do not use `uv run` here (it would re-sync dev deps at start-up)
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
