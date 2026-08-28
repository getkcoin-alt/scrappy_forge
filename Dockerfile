FROM python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /release . \
    && python -m pip install --no-cache-dir --prefix /install '/release/scrappy_forge-0.4.0-py3-none-any.whl[server]'

FROM python:3.12-slim
COPY --from=build /install /usr/local
COPY --from=build /release /app/releases
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 FORGE_RELEASE_DIR=/app/releases
USER 10001:10001
EXPOSE 8080
CMD ["forge-hub"]
