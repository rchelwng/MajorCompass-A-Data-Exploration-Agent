FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app.py context_panel.py context-panel.js index.html ./
COPY data/majors.duckdb data/majors.duckdb
ENV HOST=0.0.0.0
CMD ["sh", "-c", "uv run --no-sync python app.py"]
