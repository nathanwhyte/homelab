# ARC runner image for webway's CI (Elixir/Phoenix umbrella, PROJ-1010).
#
# Layered on the compendium runner image (../runner/) so the shared layers —
# runner release, node, gh, uv, CPython — are cached once per node. Only the
# webway scale set uses this image; compendium's stays unchanged.
#
# Additions:
#   - Erlang/OTP + Elixir: precompiled builds (the same ones erlef/setup-beam
#     downloads), baked in because ephemeral pods have no tool cache.
#   - PostgreSQL server: the scale set runs without containerMode (no Docker,
#     so no `services:` containers) and runner egress cannot reach cluster
#     Postgres, so CI starts a throwaway server inside the job with initdb +
#     pg_ctl as the unprivileged runner user.
#   - Hex and rebar3 for the runner user, so `mix deps.get` needs no bootstrap.
#
# Bump OTP_VERSION / ELIXIR_VERSION / POSTGRES_MAJOR together with webway's
# local toolchain and docker-compose.yml; build-push.sh tags the result.
ARG BASE_IMAGE=registry.nathanwhyte.dev/ci/actions-runner:latest
FROM ${BASE_IMAGE}

ARG OTP_VERSION=29.1.1
ARG ELIXIR_VERSION=1.20.4
ARG ELIXIR_OTP_MAJOR=29
ARG POSTGRES_MAJOR=18

USER root

# unzip for the Elixir release; libncurses6/libssl3 are OTP runtime deps.
# PostgreSQL from the PGDG apt repo (noble's own archive lags the major).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        unzip \
        libncurses6 \
        libssl3 \
        locales \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-${POSTGRES_MAJOR}" \
    && rm -rf /var/lib/apt/lists/*

# Erlang/OTP from builds.hex.pm, installed under /opt/otp.
RUN curl -fsSL "https://builds.hex.pm/builds/otp/amd64/ubuntu-24.04/OTP-${OTP_VERSION}.tar.gz" \
        -o /tmp/otp.tar.gz \
    && mkdir -p /opt/otp \
    && tar -xzf /tmp/otp.tar.gz -C /opt/otp --strip-components=1 \
    && /opt/otp/Install -minimal /opt/otp \
    && rm /tmp/otp.tar.gz

# Elixir precompiled for the matching OTP major, under /opt/elixir.
RUN curl -fsSL "https://github.com/elixir-lang/elixir/releases/download/v${ELIXIR_VERSION}/elixir-otp-${ELIXIR_OTP_MAJOR}.zip" \
        -o /tmp/elixir.zip \
    && unzip -q /tmp/elixir.zip -d /opt/elixir \
    && rm /tmp/elixir.zip

# Job steps inherit the container env, not shell rc files, so PATH goes here.
# The PostgreSQL bin dir is versioned and not on PATH by default.
ENV PATH="/opt/elixir/bin:/opt/otp/bin:/usr/lib/postgresql/${POSTGRES_MAJOR}/bin:${PATH}" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    ELIXIR_ERL_OPTIONS="+fnu"

USER runner

# Hex + rebar3 into the runner user's MIX_HOME (/home/runner/.mix).
#
# ERL_FLAGS=+JMsingle for this build step only: build-push.sh runs on the
# arm64 MacBook under QEMU amd64 emulation, where the BEAM JIT cannot
# dual-map its code memory and every `erl` crashes at boot (prim_tty
# nif_error undef). The cluster nodes are native amd64 and don't need it, so
# it is not set in the image's ENV.
RUN ERL_FLAGS="+JMsingle true" sh -c 'mix local.hex --force && mix local.rebar --force \
    && elixir --version' \
    && postgres --version
