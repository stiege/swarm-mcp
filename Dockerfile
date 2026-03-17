FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Claude binary is copied from the host at build time
COPY claude /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# Use the existing ubuntu user (UID 1000) to match host file ownership
RUN mkdir -p /output /workspace && chown ubuntu:ubuntu /output /workspace
RUN mkdir -p /home/ubuntu && chown ubuntu:ubuntu /home/ubuntu

USER ubuntu
WORKDIR /workspace

ENTRYPOINT ["claude"]
