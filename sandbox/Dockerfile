# CTF solver sandbox image. Generic analysis utilities; no challenge-specific code.
# Build once: docker build -t ctf-solver-sandbox:v1 sandbox/
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

# Standard file/binary inspection utilities + a non-root user.
# binwalk comes from Debian: the PyPI "binwalk" 2.1.0 package is incomplete (no binwalk.core).
RUN apt-get update && apt-get install -y --no-install-recommends \
        file binutils xxd coreutils procps net-tools binwalk curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 -s /bin/bash solver

# Standard Python libraries the agent may reach for.
RUN pip install --no-cache-dir pwntools pycryptodome requests sympy

# The container runs with a read-only root filesystem; point caches at the /tmp tmpfs and keep
# pwntools from trying to drive a terminal.
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache PWNLIB_NOTERM=1

WORKDIR /workspace
USER solver
CMD ["sleep", "infinity"]
