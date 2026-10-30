# syntax=docker/dockerfile:1
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS dependencies
WORKDIR /build
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: --target /opt/dependencies -r requirements.lock

FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS runtime
WORKDIR /opt/cityverse
RUN groupadd --gid 1337 callback && useradd --uid 1337 --gid 1337 --no-create-home callback
COPY --from=dependencies /opt/dependencies /opt/dependencies
ENV PYTHONPATH=/opt/dependencies:/opt/cityverse PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1337:1337
EXPOSE 9082
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9082/health', timeout=2).read()"]
# Source is mounted read-only from the separately audited callback ConfigMap.
CMD ["python", "-m", "backend.onprem.api.policy_callback_asgi", "--host", "0.0.0.0", "--port", "9082", "--state", "/state/callback.sqlite"]
