# syntax=docker/dockerfile:1.7
##############################################################################
# Frontend assets
#
# frontend/assets/ (compiled Tailwind CSS, the self-hosted Cairo font
# subsets, the SVG icon sprite) is generated locally with `cd build && npm
# run build` and committed to the repo like any other source file — it is
# NOT rebuilt during the Docker build. This keeps the image build to a
# single toolchain (Python) and removes an entire class of platform-specific
# failure (Node/npm inside the builder, a build/ directory that didn't make
# it into a commit, registry hiccups, etc.).
#
# Rebuild and re-commit frontend/assets/ whenever frontend/index.html,
# frontend/app.js, or anything under build/ changes:
#
#     cd build && npm install && npm run build
#     git add frontend/assets && git commit -m "rebuild frontend assets"
#
##############################################################################

##############################################################################
# Stage 1 — python dependencies
# Wheels are built here so the runtime image carries no compiler.
##############################################################################
FROM python:3.12-slim-bookworm AS deps

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt


##############################################################################
# Stage 2 — runtime
##############################################################################
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/ilw \
    ILW_TMP_ROOT=/var/tmp/iloveword

# libreoffice-writer-nogui keeps the conversion engine without the desktop
# stack. The font packages matter: LibreOffice silently renders tofu for
# Arabic text without them, which looks like a bug in our code.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui \
        libreoffice-core-nogui \
        default-jre-headless \
        fonts-dejavu-core \
        fonts-liberation2 \
        fonts-noto-core \
        fonts-kacst \
        fonts-hosny-amiri \
        tesseract-ocr \
        tesseract-ocr-ara \
        tesseract-ocr-eng \
        tesseract-ocr-fra \
        tesseract-ocr-spa \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Unprivileged, no login shell. Everything the app writes lives under
# /var/tmp/iloveword, which compose mounts as a size-capped tmpfs.
RUN groupadd --system --gid 10001 ilw \
    && useradd --system --uid 10001 --gid ilw --home-dir /home/ilw \
       --shell /usr/sbin/nologin --create-home ilw

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root backend/ /app/backend/
COPY --chown=root:root frontend/index.html frontend/app.js /app/frontend/
COPY --chown=root:root frontend/assets/ /app/frontend/assets/

# Application code is read-only to the user that runs it: a successful RCE in
# a document parser still cannot rewrite the app.
RUN chmod -R a-w /app \
    && mkdir -p /var/tmp/iloveword \
    && chown ilw:ilw /var/tmp/iloveword

USER ilw
WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# --proxy-headers + --forwarded-allow-ips make get_remote_address see the real
# client instead of the nginx container, which is what makes rate limiting work.
# Workers stay at 1 by default: each conversion already uses several threads and
# LibreOffice is the real memory consumer. Raise it only with Redis-backed
# rate-limit storage (ILW_RATE_STORAGE_URI), never with memory://.
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "30", \
     "--no-server-header", \
     "--log-level", "info"]


##############################################################################
# Stage 3 — web (nginx + the built frontend)
# Static files are the same committed frontend/assets/ the app stage copies, so
# page nginx serves can never drift from the API it proxies to.
##############################################################################
FROM nginx:1.27-alpine AS web

COPY frontend/assets/ /usr/share/nginx/html/assets/
COPY frontend/index.html frontend/app.js /usr/share/nginx/html/
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/proxy_params /etc/nginx/proxy_params_inline
COPY nginx/security_headers.conf /etc/nginx/security_headers.conf

EXPOSE 80 443
