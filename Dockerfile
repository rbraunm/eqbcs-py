# syntax=docker/dockerfile:1.7
FROM python:3.13-slim
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    EQBCS_PORT_RANGE_START=22112 \
    EQBCS_SERVER_COUNT=1

RUN set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    bash ca-certificates tzdata \
  ; \
  rm -rf /var/lib/apt/lists/*; \
  groupadd -g 1000 eqbcs; \
  useradd -r -u 1000 -g eqbcs -d /app -m eqbcs; \
  chown -R eqbcs:eqbcs /app

USER eqbcs
WORKDIR /app

COPY --chown=eqbcs:eqbcs --chmod=0755 eqbcs.py /app/eqbcs.py
COPY --chown=eqbcs:eqbcs --chmod=0755 entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
