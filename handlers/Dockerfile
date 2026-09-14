# Deliberately standalone: this image must not import Dograh's `api` package,
# or it drags in pipecat and the whole voice dependency tree and stops being a
# small container that restarts in seconds.
FROM python:3.13-slim

WORKDIR /app

RUN groupadd --system handlers \
 && useradd --system --gid handlers --no-log-init --home-dir /app --shell /usr/sbin/nologin handlers

COPY handlers/requirements.txt ./handlers/requirements.txt
RUN pip install --no-cache-dir -r handlers/requirements.txt

COPY --chown=handlers:handlers ./handlers ./handlers

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

USER handlers
EXPOSE 8080

CMD ["uvicorn", "handlers.main:app", "--host", "0.0.0.0", "--port", "8080"]
