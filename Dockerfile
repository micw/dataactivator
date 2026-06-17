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

# Unprivileged user; data and config live under the mounted working dir.
RUN useradd --create-home --uid 1000 app
WORKDIR /app
RUN chown app:app /app
USER app

# config.yaml and data/ are expected to be mounted here at runtime, e.g.
#   docker run -v ./config.yaml:/app/config.yaml -v ./data:/app/data ...
VOLUME ["/app/data"]

ENTRYPOINT ["dataactivator"]
CMD ["watch"]
