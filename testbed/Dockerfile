# One image for all four services; the compose `command` picks the module.
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
# requirements.lock is `uv export --frozen --no-dev --no-emit-project` (hash-pinned).
COPY testbed/requirements.lock .
RUN pip install --require-hashes --only-binary=:all: -r requirements.lock

COPY testbed/app ./app
RUN useradd --system --uid 10001 svc
USER svc
EXPOSE 8000
