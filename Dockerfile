FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-server.lock ./
RUN pip install --no-cache-dir -r requirements-server.lock \
    && groupadd --gid 1000 broute \
    && useradd --uid 1000 --gid 1000 --no-create-home broute \
    && mkdir /app/runtime-data /app/backups \
    && chown broute:broute /app/runtime-data /app/backups
COPY --chown=1000:1000 scripts ./scripts
COPY --chown=1000:1000 data ./data
USER 1000:1000
ENTRYPOINT ["python", "scripts/server_runtime.py"]
CMD ["--help"]
