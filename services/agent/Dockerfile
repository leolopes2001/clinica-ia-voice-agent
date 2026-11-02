FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

WORKDIR /app
# O workspace precisa do pyproject.toml de todos os membros.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev --package clinica-agent

RUN useradd --create-home clinica
USER clinica

# Sem isso, o ADK espera ~12 s pelo servidor de metadados do Google Cloud em cada ligação.
ENV PATH="/app/.venv/bin:$PATH" NO_GCE_CHECK=true
CMD ["python", "-m", "clinica_agent", "start"]
