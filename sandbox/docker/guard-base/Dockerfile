# SPDX-License-Identifier: Apache-2.0
ARG PYTHON_BASE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
FROM ${PYTHON_BASE}

ARG IPTABLES_VERSION=1.8.11-2
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       "iptables=${IPTABLES_VERSION}" \
    && rm -rf \
       /var/cache/apt/* \
       /var/cache/ldconfig/* \
       /var/lib/apt/lists/* \
       /var/log/alternatives.log \
       /var/log/apt/* \
       /var/log/dpkg.log
