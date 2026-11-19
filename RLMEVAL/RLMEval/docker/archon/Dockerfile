FROM python:3.12-bookworm@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267 AS archon-base

RUN useradd --create-home --uid 1000 --shell /bin/bash rlm

COPY archon /opt/archon
RUN pip install --no-cache-dir /opt/archon

FROM archon-base

COPY install_claude.py /opt/rlm/install_claude.py
RUN python /opt/rlm/install_claude.py && claude --version

COPY codex /opt/codex
RUN ln -s /opt/codex/bin/codex /usr/local/bin/codex \
    && ln -s /opt/codex/codex-path/rg /usr/local/bin/rg \
    && printf '#!/bin/sh\nexec /opt/codex/bin/codex --codex-run-as-apply-patch "$@"\n' > /usr/local/bin/apply_patch \
    && chmod +x /usr/local/bin/apply_patch

COPY toolchains /opt/elan/toolchains
COPY environments /opt/rlm/envs
COPY rlm_pipeline /opt/rlm/src/rlm_pipeline
COPY build-manifest.json /opt/rlm/build-manifest.json
COPY api-profiles.json /opt/rlm/api-profiles.json
RUN mkdir -p /outputs \
    && git config --system user.name 'RLMEval' \
    && git config --system user.email 'rlmeval@localhost'

ENV ARCHON_INFORMAL_MAIN_ONLY=1 \
    PYTHONPATH=/opt/rlm/src \
    PYTHONUNBUFFERED=1 \
    ELAN_HOME=/opt/elan \
    RLM_ENVS_ROOT=/opt/rlm/envs \
    RLM_AGENT_USER=rlm

WORKDIR /outputs
ENTRYPOINT ["python", "-m", "rlm_pipeline"]
