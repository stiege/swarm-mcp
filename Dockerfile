FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git jq \
    python3 python3-venv \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install uv from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Claude Code CLI via official install script, then copy to system path
RUN curl -fsSL https://claude.ai/install.sh | bash && \
    cp -L /root/.local/bin/claude /usr/local/bin/claude && \
    rm -rf /root/.local /root/.claude

# Use the existing ubuntu user (UID 1000) to match host file ownership
RUN mkdir -p /output /workspace && chown ubuntu:ubuntu /output /workspace
RUN mkdir -p /home/ubuntu && chown ubuntu:ubuntu /home/ubuntu

USER ubuntu
WORKDIR /workspace

ENTRYPOINT ["claude"]
