# SPDX-License-Identifier: Apache-2.0
# Build with the narrow context: docker build -f agents/Dockerfile agents
ARG PYTHON_BASE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_BASE}

WORKDIR /app
COPY --chown=65532:65532 \
    __init__.py common.py cost.py evidence.py evoskill.py llm.py manifest.py \
    prompt.py reckless.py server.py \
    /app/agents/
COPY --chown=65532:65532 prompts/baseline-system.txt \
    /app/agents/prompts/baseline-system.txt

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADEVOLVE_AGENT_HOST=0.0.0.0 \
    TRADEVOLVE_AGENT_PORT=8000

USER 65532:65532
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=2s --retries=12 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=1).read()"]

CMD ["python", "-m", "agents.server"]
