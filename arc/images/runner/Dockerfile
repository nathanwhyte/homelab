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
# setup-uv / setup-python actions still work on top of this; pre-installing uv
# just makes the ephemeral pods cheap when caches miss.
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

# GitHub CLI — the compendium batch-runner e2e tests (make test) shell out to
# `gh`; ubuntu-latest ships it, so the custom image must too.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# uv, system-wide: installer defaults target $HOME/.local/bin, which is NOT on
# the runner's job-time PATH (jobs inherit the runner env, not shell rc files).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Must end as the runner user (uid 1001); entrypoint /home/runner/run.sh and
# /home/runner/externals (bundled Node runtimes) come from the base image.
USER runner
