FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git jq python3 python3-venv \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Claude and uv binaries copied from host at build time
COPY claude /usr/local/bin/claude
COPY uv /usr/local/bin/uv
RUN chmod +x /usr/local/bin/claude /usr/local/bin/uv

# Use the existing ubuntu user (UID 1000) to match host file ownership
RUN mkdir -p /output /workspace && chown ubuntu:ubuntu /output /workspace
RUN mkdir -p /home/ubuntu && chown ubuntu:ubuntu /home/ubuntu

USER ubuntu
WORKDIR /workspace

ENTRYPOINT ["claude"]
