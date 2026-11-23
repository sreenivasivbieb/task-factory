FROM python:3.11-slim
USER root
RUN command -v git >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*) || apk add --no-cache git || yum install -y git
RUN id factory >/dev/null 2>&1 || useradd -m -u 10001 factory 2>/dev/null || adduser -D -u 10001 factory 2>/dev/null || true
WORKDIR /repo
ADD repo.tar /repo/
RUN git init -q && git add -A && git -c user.name=factory -c user.email=factory@localhost -c commit.gpgsign=false commit -q --allow-empty -m base && git tag factory-base && mkdir -p .factory && printf '%s\n' .factory/ '*.egg-info/' node_modules/ __pycache__/ '.pytest_cache/' >> .git/info/exclude
RUN pip install -q -e '.[test]'
RUN pip install -q pytest
RUN (id factory >/dev/null 2>&1 && chown -R factory /repo) || true
