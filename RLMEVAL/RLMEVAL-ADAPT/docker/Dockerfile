FROM python:3.12-bookworm
RUN --mount=type=bind,source=wheels,target=/wheels \
    pip install --no-index --find-links=/wheels jsonlines PyYAML lean-interact==0.5.3 nltk rich \
    && useradd --uid 1000 --create-home benchmark \
    && mkdir /workspace /cache_dir \
    && chown benchmark:benchmark /workspace /cache_dir
COPY toolchains/ /opt/elan/toolchains/
COPY envs/ /opt/rlm/envs/
COPY codex/bin/ /usr/local/bin/
COPY codex/codex-path/ /usr/local/bin/
COPY claude /usr/local/bin/claude
RUN printf '#!/bin/sh\nexec codex --codex-run-as-apply-patch "$@"\n' > /usr/local/bin/apply_patch \
    && chmod +x /usr/local/bin/apply_patch
COPY src /opt/rlmeval/src
COPY run-task.py codex-container.sh /opt/adapter/
ENV PYTHONPATH=/opt/rlmeval/src ELAN_HOME=/opt/elan RLM_MASK_PATHS="" PYTHONUNBUFFERED=1
USER benchmark
ENTRYPOINT ["python", "/opt/adapter/run-task.py"]
