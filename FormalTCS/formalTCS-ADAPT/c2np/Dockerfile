FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd
ARG TARGETARCH
COPY formalTCS-ADAPT/c2np/install_claude.py /opt/formaltcs/install_claude.py
RUN python -B /opt/formaltcs/install_claude.py "$TARGETARCH" /usr/local/bin/claude && claude --version
COPY formalTCS-ADAPT/c2np/run.py formalTCS-ADAPT/c2np/docker_worker.py /opt/formaltcs/
WORKDIR /workspace
USER 65534:65534
ENTRYPOINT ["python", "-B", "/opt/formaltcs/docker_worker.py"]
