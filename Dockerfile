FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src

# Build a wheel so the runtime image stays free of build tooling.
RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist


FROM python:3.12-slim

# Runtime-only: install the prebuilt wheel, no source tree or compilers.
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Unprivileged user. Two mounts at runtime:
#   /config  -> the config file (Secret; it holds credentials) at /config/config.yaml
#   /data    -> a PersistentVolume for the event log, datasets and sessions;
#               set `storage.folder: /data` in the config.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data /config \
    && chown -R app:app /data
WORKDIR /data
USER app

VOLUME ["/data"]

# Default command: serve (watch all configured providers) with the config
# mounted at /config/config.yaml. Override args in the pod spec if needed.
ENTRYPOINT ["dataactivator"]
CMD ["-c", "/config/config.yaml", "serve"]
