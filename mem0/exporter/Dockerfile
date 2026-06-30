FROM python:3.12-slim

WORKDIR /app

# psycopg[binary] keeps the image independent of Debian libpq package drift while
# avoiding runtime package installs in the Kubernetes pod.
RUN pip install --no-cache-dir 'psycopg[binary]==3.2.13'

COPY --chown=65532:65532 --chmod=0555 mem0_exporter.py /app/mem0_exporter.py

ENV PYTHONUNBUFFERED=1
EXPOSE 9090

USER 65532:65532

CMD ["python", "/app/mem0_exporter.py"]
