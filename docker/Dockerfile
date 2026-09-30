# Storepulse hosted mode (docs/SPEC.md, Phase 4). Build from the repository root:
#   docker build -f docker/Dockerfile -t storepulse .
# Published as ghcr.io/romol4/storepulse by .github/workflows/release.yml.

# python:3.12-slim (Dependabot bumps this digest; a trailing comment on FROM isn't reliable)
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS build
WORKDIR /src
RUN pip install --no-cache-dir "build>=1.2,<2"
COPY pyproject.toml README.md LICENSE requirements-dev.lock ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

# python:3.12-slim (Dependabot bumps this digest; a trailing comment on FROM isn't reliable)
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
LABEL org.opencontainers.image.source="https://github.com/romol4/storepulse" \
      org.opencontainers.image.description="Storepulse hosted mode: App Store + Google Play stats, daily email and dashboard" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STOREPULSE_MODE=hosted \
    STOREPULSE_DATA_DIR=/data \
    STOREPULSE_CONFIG_DIR=/data/config
# Web dependencies are held to the exact versions CI tests (requirements-dev.lock).
COPY --from=build /dist /dist
COPY requirements-dev.lock /tmp/constraints.txt
# The pinned digest above lags Debian's security patches until Dependabot bumps it;
# this installs today's point-releases so the image Trivy scans isn't stuck waiting on that.
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* \
 && wheel="$(ls /dist/storepulse-*.whl)" \
 && pip install --no-cache-dir --constraint /tmp/constraints.txt "${wheel}[web]" \
 && rm -rf /dist /tmp/constraints.txt \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin storepulse \
 && mkdir -p /data/config \
 && chown -R storepulse /data
USER storepulse
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["storepulse", "serve", "--host", "0.0.0.0", "--port", "8000"]
