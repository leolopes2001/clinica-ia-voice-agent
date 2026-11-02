FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

WORKDIR /app
# O workspace precisa do pyproject.toml de todos os membros.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev --package clinica-api

RUN useradd --system --no-create-home clinica
USER clinica

WORKDIR /app/services/api
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory clinica_api.app:criar_app --host 0.0.0.0 --port 8000"]
