# CTF solver sandbox image. Generic analysis utilities; no challenge-specific code.
# Build once: docker build -t ctf-solver-sandbox:v1 sandbox/
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

# Standard file/binary inspection utilities + a non-root user.
RUN apt-get update && apt-get install -y --no-install-recommends \
        file binutils xxd coreutils procps net-tools \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash solver

# Standard Python libraries the agent may reach for.
RUN pip install --no-cache-dir pycryptodome requests sympy binwalk

WORKDIR /workspace
USER solver
CMD ["sleep", "infinity"]
