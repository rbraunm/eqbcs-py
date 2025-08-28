# syntax=docker/dockerfile:1.7
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive

# 1) Repos + i386 + core deps (Wine, Python, winetricks, headless X) + app user/dirs
RUN set -eux; \
  if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -ri 's/^Components: .*/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources; \
  else \
    sed -ri 's/ main/ main contrib non-free non-free-firmware/g' /etc/apt/sources.list; \
  fi; \
  dpkg --add-architecture i386; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    tzdata ca-certificates \
    wine wine32:i386 wine64 libwine libwine:i386 \
    winbind \
    winetricks cabextract \
    xvfb xauth \
  ; \
  rm -rf /var/lib/apt/lists/*; \
  groupadd -g 1000 eqbcs; \
  useradd -r -u 1000 -g eqbcs -d /app -m eqbcs; \
  mkdir -p /app/wineprefix /app/eqbcs /app/wineprefixes/bootstrap /app/.cache; \
  chown -R eqbcs:eqbcs /app

USER eqbcs

# 2) Runtime env
ENV LANG=C.UTF-8 \
    WINEDEBUG=-all \
    WINEDLLOVERRIDES=mscoree,mshtml= \
    WINEPREFIX=/app/wineprefix \
    BOOTSTRAP_WINEPREFIX=/app/wineprefixes/bootstrap \
    XDG_CACHE_HOME=/app/.cache \
    EQBCS_PORT_RANGE_START=22112 \
    EQBCS_SERVER_COUNT=1

# 3) Headless bootstrap of the 32-bit prefix + vcrun6
RUN set -eux; \
  xvfb-run -a bash -lc '\
    export WINEARCH=win32 WINEPREFIX="$BOOTSTRAP_WINEPREFIX"; \
    wineboot -u; \
    winetricks -q vcrun6 \
  '; \
  rm -rf "$XDG_CACHE_HOME/winetricks" || true

WORKDIR /app

# 4) App + entrypoint
COPY --chown=eqbcs:eqbcs app/ /app/eqbcs/
COPY --chown=eqbcs:eqbcs --chmod=0755 entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
