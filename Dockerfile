# syntax=docker/dockerfile:1.7
FROM python:3.13-slim
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    EQBCS_PY_PORT_RANGE_START=22112 \
    EQBCS_PY_SERVER_COUNT=1

RUN set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    bash ca-certificates tzdata \
  ; \
  rm -rf /var/lib/apt/lists/*; \
  groupadd -g 1000 eqbcs-py; \
  useradd -r -u 1000 -g eqbcs-py -d /app -m eqbcs-py; \
  chown -R eqbcs-py:eqbcs-py /app

USER eqbcs-py
WORKDIR /app

COPY --chown=eqbcs-py:eqbcs-py --chmod=0755 server.py /app/server.py
COPY --chown=eqbcs-py:eqbcs-py --chmod=0755 entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
