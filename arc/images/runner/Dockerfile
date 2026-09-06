# Custom ARC runner image for the compendium vault-checks workflow (IDEA-1094).
#
# Base: GitHub's official runner image (Debian-based, user `runner` uid 1001,
# entrypoint expectations baked in — derived images must keep USER runner and
# must not override ENTRYPOINT).
#
# Additions over the base image, driven by compendium's Makefile + pre-commit:
#   - uv           (make test / make lint run everything through `uv run`)
#   - node         (pre-commit's markdownlint-cli2 hook is language: node;
#                   a system node avoids a nodeenv download on every ephemeral pod)
#   - make, build tools
#   - CPython 3.12  (uv-managed; replaces actions/setup-python, which had to
#                   download the interpreter on every ephemeral pod — IMPR-1131)
# setup-uv still works on top of this; pre-installing uv just makes the
# ephemeral pods cheap when caches miss.
# Pin the runner release: it determines which Node runtimes JS actions get
# (Node 20 is removed from the runner on 2026-09-23 — self-hosted doesn't
# sidestep that, but the pin controls when we take the jump).
ARG RUNNER_VERSION=2.337.0
FROM ghcr.io/actions/actions-runner:${RUNNER_VERSION}

USER root

# Base is Ubuntu 24.04 (noble) with sparse apt state — update before install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        make \
        git \
        curl \
        ca-certificates \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Node 24 from nodesource (noble's apt nodejs is 18, EOL).
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# prettier, global: the batch runner (and its e2e suite in make test) formats
# with `npx prettier`, and npx resolves a global install without touching the
# npm cache — verified in this image, no ~/.npm/_npx download. Without it,
# every ephemeral pod fetched prettier from the registry on first use
# (IMPR-1131). Pin exact; bump deliberately.
RUN npm install -g prettier@3.9.6 && npm cache clean --force

# GitHub CLI — the compendium batch-runner e2e tests (make test) shell out to
# `gh`; ubuntu-latest ships it, so the custom image must too.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# uv + uvx, system-wide and pinned to a release via the official image (IMPR-1131):
# astral.sh's install.sh fetches an unversioned "latest", so copy the static
# binaries instead — reproducible, offline-buildable, and version-controlled by
# the tag. /bin is on the runner's job-time PATH; the installer default
# $HOME/.local/bin is NOT (jobs inherit the runner env, not shell rc files).
# Needs BuildKit: COPY --from=<image> is not supported by the legacy builder.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

# Must end as the runner user (uid 1001); entrypoint /home/runner/run.sh and
# /home/runner/externals (bundled Node runtimes) come from the base image.
USER runner

# CPython, uv-managed, installed as the runner user into uv's default
# UV_PYTHON_INSTALL_DIR (/home/runner/.local/share/uv/python — the same path
# setup-uv exports at job time, so `uv run` finds it with no action step).
# Replaces actions/setup-python in vault-checks: ephemeral pods have no tool
# cache, so that action re-downloaded and re-installed CPython on every job
# (~15s/job, compendium IMPR-1131). Bump PYTHON_VERSION together with the
# vault's .python-version.
ARG PYTHON_VERSION=3.12
RUN uv python install "${PYTHON_VERSION}"
# Fail loudly if a job asks for a Python this image does not carry, instead of
# silently re-introducing a per-job download.
ENV UV_PYTHON_DOWNLOADS=never
