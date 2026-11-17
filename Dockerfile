# Image for the worker (`app`) and the dashboard. Verified in T347/T349: it builds on
# arm64 and Chromium renders real decks. Base is python:3.13-slim (Debian trixie)
# because pyproject.toml needs Python >=3.13 and Microsoft's playwright images
# bundle an older one; `playwright install --with-deps` does the apt work.
FROM python:3.13-slim

ARG TARGETARCH
# Pinned cloudflared release; sha256 from the GitHub release asset digests.
ARG CLOUDFLARED_VERSION=2026.10.7
ARG CLOUDFLARED_SHA256_AMD64=70b1a1e0f46033ffc8d90b406e554044ba6cbf2e4360295ef1163aed31e09393
ARG CLOUDFLARED_SHA256_ARM64=d9407eb6e70ac31f54978a6b408a0320b51b664a6d0b498248692d7ac56bf836
# Gelasio (Georgia look-alike) is not packaged by Debian: pinned google/fonts commit.
ARG GELASIO_COMMIT=86aced91de05cd4f96a06838124cca45be337b2b
ARG GELASIO_SHA256=4daecea457258c9ebeb8bc99ed3fd24353618bfad3ea4b93fa0b5d0468fc04e4
ARG GELASIO_ITALIC_SHA256=52559e845a4d33514e5f93bb9ae7dbeae1894a53f2c565a15f18af40cd337c09

# tzdata: TZ support; sqlite3: ops CLI (runbook); curl: fetches below;
# libatomic1: playwright>=1.65 node driver links it (T363);
# ffmpeg: a still from a channel video for the vision model (T375);
# fonts-inter / fonts-jetbrains-mono: open look-alikes for SF / Menlo (U13).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata sqlite3 fontconfig libatomic1 ffmpeg \
      fonts-inter fonts-jetbrains-mono \
 && case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      arm64) CF_SHA="$CLOUDFLARED_SHA256_ARM64"; CF_ARCH=arm64 ;; \
      amd64) CF_SHA="$CLOUDFLARED_SHA256_AMD64"; CF_ARCH=amd64 ;; \
      *) echo "unsupported arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /tmp/cloudflared.deb \
      "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${CF_ARCH}.deb" \
 && echo "${CF_SHA}  /tmp/cloudflared.deb" | sha256sum -c - \
 && apt-get install -y --no-install-recommends /tmp/cloudflared.deb \
 && mkdir -p /usr/local/share/fonts/gelasio \
 && curl -fsSL -o "/usr/local/share/fonts/gelasio/Gelasio[wght].ttf" \
      "https://raw.githubusercontent.com/google/fonts/${GELASIO_COMMIT}/ofl/gelasio/Gelasio%5Bwght%5D.ttf" \
 && curl -fsSL -o "/usr/local/share/fonts/gelasio/Gelasio-Italic[wght].ttf" \
      "https://raw.githubusercontent.com/google/fonts/${GELASIO_COMMIT}/ofl/gelasio/Gelasio-Italic%5Bwght%5D.ttf" \
 && echo "${GELASIO_SHA256}  /usr/local/share/fonts/gelasio/Gelasio[wght].ttf" | sha256sum -c - \
 && echo "${GELASIO_ITALIC_SHA256}  /usr/local/share/fonts/gelasio/Gelasio-Italic[wght].ttf" | sha256sum -c - \
 && rm -rf /tmp/cloudflared.deb /var/lib/apt/lists/*
COPY deploy/docker/fonts.conf /etc/fonts/conf.d/60-apple-lookalikes.conf
# Patrick Hand (OFL, already in the repo) as the system-wide open fallback for the
# sketchbook theme's handwriting stack ('Chalkboard SE' / 'Bradley Hand' are Apple-only).
COPY config/fonts/doodle/PatrickHand-Regular.ttf /usr/local/share/fonts/doodle/PatrickHand-Regular.ttf
RUN fc-cache -f

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
# D312: themes, fonts and templates ship in the image; compose mounts (:ro) may override.
COPY config ./config
COPY templates ./templates
RUN pip install --no-cache-dir . && rm -rf /app/build /app/*.egg-info && playwright install --with-deps chromium

ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PIPELINE_IN_CONTAINER=1
CMD ["python", "-m", "pipeline"]
# Ollama deliberately runs on the HOST, not here: Docker Desktop on macOS
# cannot reach the GPU, and CPU inference is roughly 10x slower.
# The app container reaches it at host.docker.internal:11434.
