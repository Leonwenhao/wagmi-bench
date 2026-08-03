# SPDX-License-Identifier: Apache-2.0
ARG GUARD_BASE
FROM ${GUARD_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/tradeevolve
COPY __init__.py block_collector.py firewall_runtime.py guard_main.py \
    /opt/tradeevolve/tradeevolve_guard/

# The supported launcher adds only CAP_NET_ADMIN, CAP_SETGID, and CAP_SETUID.
# guard_main installs and verifies namespace rules, drops to 65533:65533 with
# zero effective capabilities, then starts collectors.
USER 0:0
HEALTHCHECK --interval=2s --timeout=2s --retries=15 \
    CMD ["python3", "-m", "tradeevolve_guard.guard_main", "--check-ready"]
ENTRYPOINT ["python3", "-m", "tradeevolve_guard.guard_main"]
