# syntax=docker/dockerfile:1
# Existing resolved base: the pinned FROM instruction in Dockerfile (not a fabricated worker digest).
FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS sources
WORKDIR /opt/cityverse
COPY archive/legacy/evidence/capture_worker.py archive/legacy/evidence/capture_contract.py ./archive/legacy/evidence/
COPY archive/legacy/policy/policy_observer.py ./archive/legacy/policy/

FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS runtime
WORKDIR /opt/cityverse
RUN mkdir -p /run/cityverse-capture /run/cityverse-capture-key /etc/cityverse
COPY --from=sources /opt/cityverse/archive ./archive
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Reviewed exception: non-root Docker exec lost NET_RAW (see README).
# Runtime drops ALL capabilities and grants NET_RAW only, never privileged.
USER 0:0
# No HTTP/listener. Only fixed authenticated kubectl-exec starts a worker.
HEALTHCHECK NONE
CMD ["python", "-c", "import signal,sys;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));signal.pause()"]
