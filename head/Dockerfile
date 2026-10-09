# The head's container (design section 8.1). One process, one worker: the stores are in memory.
FROM python:3.12-slim

ARG PORTHOLE_VERSION=""
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/head \
    PORTHOLE_VERSION=${PORTHOLE_VERSION}

WORKDIR /app
COPY head/requirements.txt /app/head/requirements.txt
RUN pip install --no-cache-dir -r /app/head/requirements.txt \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin porthole

COPY harness/insights_harness.py /app/
COPY watcher/ /app/watcher/
COPY head/ /app/head/

USER porthole
EXPOSE 8080
CMD ["uvicorn", "porthole.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
