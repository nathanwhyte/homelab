#!/usr/bin/env python3
"""HTTP-only recovery shared by the host preparation module and generated job.

The host publishes revision-specific witness tags only after runtime verification.
Recovery never creates recipes, changes systemd, or evicts an existing runner.
"""

import json
import os
import re
import shlex
import urllib.error
import urllib.request


class PreparationError(RuntimeError):
    """The requested posture has not been proved."""


def digest(value):
    value = str(value).removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PreparationError("missing or malformed model digest")
    return value


class Http:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def call(self, path, body=None, method=None):
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path,
            data=payload,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(
                request, timeout=300 if path == "/api/generate" else 15
            ) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise PreparationError(f"oversized {path} response")
            result = json.loads(raw) if raw else {}
        except (OSError, ValueError) as exc:
            raise PreparationError(f"{path}: {exc}") from exc
        if not isinstance(result, dict) or result.get("error"):
            raise PreparationError(
                f"{path} did not succeed: {result.get('error') if isinstance(result, dict) else 'not an object'}"
            )
        return result

    def tags(self):
        rows = self.call("/api/tags").get("models")
        if not isinstance(rows, list):
            raise PreparationError("/api/tags has no model list")
        result = {}
        for row in rows:
            name = row.get("name")
            if not isinstance(name, str) or name in result:
                raise PreparationError("ambiguous model inventory")
            result[name] = digest(row.get("digest"))
        return result

    def show(self, name):
        return self.call("/api/show", {"model": name})

    def loaded(self):
        rows = self.call("/api/ps").get("models")
        if not isinstance(rows, list):
            raise PreparationError("/api/ps has no model list")
        return rows

    def load(self, name):
        result = self.call(
            "/api/generate", {"model": name, "keep_alive": -1, "stream": False}
        )
        if result.get("done") is not True or result.get("done_reason") != "load":
            raise PreparationError(f"{name}: load-only request did not complete")


def parameters(show):
    """Read Ollama's canonical parameter display, not a user Modelfile."""
    values = {}
    text = show.get("parameters")
    if not isinstance(text, str):
        raise PreparationError("show response lacks canonical parameters")
    for line in text.splitlines():
        fields = shlex.split(line)
        if not fields:
            continue
        if len(fields) != 2:
            raise PreparationError("malformed canonical parameter display")
        values.setdefault(fields[0], []).append(fields[1])
    return values


def integer_parameter(show, name, default=None):
    values = parameters(show).get(name)
    if values is None and default is not None:
        return default
    if values is None or len(values) != 1:
        raise PreparationError(f"missing or ambiguous {name}")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise PreparationError(f"invalid {name}") from exc
    if value <= 0:
        raise PreparationError(f"nonpositive {name}")
    return value


def verify_loaded(rows, expected):
    names = [row.get("name") for row in rows]
    if len(names) != len(set(names)) or set(names) != set(expected):
        raise PreparationError(
            f"loaded models {names!r} do not match intended residents {list(expected)!r}"
        )
    for row in rows:
        want = expected[row["name"]]
        if digest(row.get("digest")) != want["digest"]:
            raise PreparationError(
                f"{row['name']}: loaded manifest differs from verified recipe"
            )
        if row.get("context_length") != want["context"]:
            raise PreparationError(f"{row['name']}: loaded context differs from recipe")
        if not isinstance(row.get("size_vram"), int) or row["size_vram"] <= 0:
            raise PreparationError(f"{row['name']}: no GPU residency observed")


def recover(config, api):
    # A running ad-hoc model belongs to its caller. Even a malformed nonempty
    # row means we cannot prove idle, so do no work.
    if api.loaded():
        return {"status": "busy", "changed": False}
    if api.call("/api/version").get("version") != config["version"]:
        raise PreparationError("daemon version does not match the recovery revision")
    inventory = api.tags()
    expected = {}
    for model, witness in config["residents"].items():
        if witness not in inventory or inventory.get(model) != inventory[witness]:
            raise PreparationError(
                f"{model}: missing or stale host verification; run the host adoption workflow"
            )
        shown = api.show(witness)
        expected[model] = {
            "digest": inventory[witness],
            "context": integer_parameter(shown, "num_ctx"),
        }
    # Narrow the courtesy race and reject a manifest change during observation.
    if api.loaded():
        return {"status": "busy", "changed": False}
    if api.tags() != inventory:
        raise PreparationError("model inventory changed during recovery preflight")
    for model in expected:
        api.load(model)
    verify_loaded(api.loaded(), expected)
    after = api.tags()
    if any(
        after.get(name) != inventory[name]
        for name in [*expected, *config["residents"].values()]
    ):
        raise PreparationError("verified model references changed during recovery")
    return {"status": "recovered", "changed": True, "models": expected}


if __name__ == "__main__":
    try:
        print(
            json.dumps(
                recover(
                    json.loads(os.environ["OLLAMA_RECOVERY"]),
                    Http(os.environ["OLLAMA_URL"]),
                )
            )
        )
    except (PreparationError, ValueError, KeyError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
