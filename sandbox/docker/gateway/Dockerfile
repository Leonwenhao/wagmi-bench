# SPDX-License-Identifier: Apache-2.0
ARG PYTHON_BASE
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/tradeevolve
COPY --chown=65532:65532 gateway_server.py /opt/tradeevolve/gateway_server.py

USER 65532:65532
ENTRYPOINT ["python3", "/opt/tradeevolve/gateway_server.py"]
