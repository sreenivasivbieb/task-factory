# The factory in a container: Python (the CLI and harness), Node (the SWE miner), git, gh, the
# Docker CLI with compose (sandboxes run on the host's Docker through its socket), Harbor 0.25 (the
# hackathon's task runner) and the opencode CLI (free models for `factory produce`).
# See docker-compose.yml.
FROM node:22-slim AS node
FROM docker:27-cli AS docker

FROM python:3.12-slim
ARG GH_VERSION=2.105.0
ARG HARBOR_VERSION=0.25.0
ARG OPENCODE_VERSION=2.4.1
ARG TARGETARCH=amd64
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates curl \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz" \
       | tar -xz -C /tmp && mv /tmp/gh_*/bin/gh /usr/local/bin/gh && rm -rf /tmp/gh_* \
    && apt-get purge -y -qq curl && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g --silent "@opencode/cli@${OPENCODE_VERSION}" && npm cache clean --force -s \
    && opencode --version
RUN pip install -q --no-cache-dir uv \
    && uv venv -q /opt/harbor && uv pip install -q --no-cache --python /opt/harbor/bin/python "harbor==${HARBOR_VERSION}" \
    && ln -s /opt/harbor/bin/harbor /usr/local/bin/harbor && harbor --version

WORKDIR /opt/task-factory
COPY pyproject.toml uv.lock README.md ./
COPY factory ./factory
COPY swe ./swe
RUN uv pip install --system --no-cache -q .

# Any user id (the host's, so files in mounted folders stay yours) gets a writable home.
RUN mkdir -p /home/tf/.local/state /home/tf/.local/share && chmod -R 1777 /home/tf
ENV HOME=/home/tf PYTHONUNBUFFERED=1 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*
# `node swe/cli.ts` works from the repository root; `swe` works anywhere.
RUN printf '#!/bin/sh\nexec node /opt/task-factory/swe/cli.ts "$@"\n' > /usr/local/bin/swe && chmod 755 /usr/local/bin/swe
WORKDIR /work
CMD ["factory", "--help"]
