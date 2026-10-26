#!/usr/bin/env python3
"""HTTP recovery using a receipt read by the host's unprivileged adapter.

Public Ollama names are not attestations. The standalone client requests recovery
from the exporter; only its local adapter reads the root-owned verification receipt.
"""

import errno
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class PreparationError(RuntimeError):
    """The requested posture has not been proved."""


class TransportUnavailable(PreparationError):
    """No usable connection yet; distinct from an HTTP/protocol rejection."""


def transient_connection_error(error):
    return isinstance(error, (ConnectionError, TimeoutError)) or (
        isinstance(error, OSError)
        and error.errno
        in {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ETIMEDOUT,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.EPIPE,
        }
    )


def digest(value):
    value = str(value).removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PreparationError("missing or malformed model digest")
    return value


class Http:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def call(self, path, body=None, method=None, timeout=None):
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path,
            data=payload,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request_timeout = (
                timeout
                if timeout is not None
                else 300
                if path == "/api/generate"
                else 15
            )
            with opener.open(request, timeout=request_timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise PreparationError(f"oversized {path} response")
            result = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            reason = ""
            try:
                with exc:
                    raw = exc.read(4097)
                if len(raw) <= 4096:
                    error = json.loads(raw)
                    if isinstance(error, dict) and isinstance(error.get("error"), str):
                        # Preserve the adapter's refusal without allowing remote
                        # control characters or unbounded text into job logs.
                        reason = ": " + json.dumps(
                            error["error"][:1024], ensure_ascii=True
                        )
            except (OSError, ValueError):
                # Diagnostic failure must not hide the original HTTP rejection.
                pass
            raise PreparationError(f"{path}: HTTP {exc.code}{reason}") from exc
        except urllib.error.URLError as exc:
            if transient_connection_error(exc.reason):
                raise TransportUnavailable(f"{path}: {exc.reason}") from exc
            raise PreparationError(f"{path}: {exc}") from exc
        except OSError as exc:
            if transient_connection_error(exc):
                raise TransportUnavailable(f"{path}: {exc}") from exc
            raise PreparationError(f"{path}: {exc}") from exc
        except ValueError as exc:
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


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {
        "revision",
        "version",
        "residents",
    }:
        raise PreparationError(
            "recovery config requires exactly revision/version/residents"
        )
    if not isinstance(config["revision"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", config["revision"]
    ):
        raise PreparationError("invalid recovery revision")
    if not isinstance(config["version"], str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", config["version"]
    ):
        raise PreparationError("invalid recovery version")
    residents = config["residents"]
    if not isinstance(residents, list) or not 1 <= len(residents) <= 32:
        raise PreparationError("recovery residents must be a nonempty list")
    if any(not valid_model_name(name) for name in residents) or len(
        set(residents)
    ) != len(residents):
        raise PreparationError("invalid or duplicate recovery resident")
    return config


def valid_model_name(name):
    return (
        isinstance(name, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9.\-]*:[a-z0-9][a-z0-9.\-]*", name) is not None
    )


def verified_models(config, receipt):
    """Validate receipt contents; filesystem provenance is the adapter's job."""
    validate_config(config)
    if not isinstance(receipt, dict) or receipt.get("schema") != 2:
        raise PreparationError("missing current host verification receipt")
    if any(
        receipt.get(key) != config[key] for key in ("revision", "version", "residents")
    ):
        raise PreparationError("host verification does not match recovery config")
    models = receipt.get("models")
    if not isinstance(models, dict) or not 1 <= len(models) <= 32:
        raise PreparationError("host verification lacks prepared models")
    for name, model in models.items():
        if not valid_model_name(name) or not isinstance(model, dict):
            raise PreparationError("invalid verified model record")
        digest(model.get("digest"))
        if type(model.get("context")) is not int or model["context"] <= 0:
            raise PreparationError("invalid verified context")
    if any(name not in models for name in config["residents"]):
        raise PreparationError("host verification is incomplete for intended residents")
    return {
        name: {
            "digest": digest(models[name]["digest"]),
            "context": models[name]["context"],
        }
        for name in config["residents"]
    }


def recover(config, api, trusted_receipt):
    expected = verified_models(config, trusted_receipt)
    # A running ad-hoc model belongs to its caller. Even a malformed nonempty
    # row means we cannot prove idle, so do no work.
    if api.loaded():
        return {"status": "busy", "changed": False}
    if api.call("/api/version").get("version") != config["version"]:
        raise PreparationError("daemon version does not match the recovery revision")
    inventory = api.tags()
    for model, wanted in expected.items():
        if inventory.get(model) != wanted["digest"]:
            raise PreparationError(
                f"{model}: missing or stale host verification; run the host adoption workflow"
            )
    # Narrow the courtesy race and reject a manifest change during observation.
    if api.loaded():
        return {"status": "busy", "changed": False}
    if api.tags() != inventory:
        raise PreparationError("model inventory changed during recovery preflight")
    completed = {}
    for model, wanted in expected.items():
        if completed:
            # Do not knowingly displace an operator's intervening load while
            # rebuilding a co-resident set. HTTP still has no atomic admission.
            verify_loaded(api.loaded(), completed)
        api.load(model)
        completed[model] = wanted
    verify_loaded(api.loaded(), expected)
    after = api.tags()
    if any(after.get(name) != wanted["digest"] for name, wanted in expected.items()):
        raise PreparationError("verified model references changed during recovery")
    return {"status": "recovered", "changed": True, "models": expected}


def request_recovery(config, url=None):
    """The cluster and CLI can request only the host's already attested posture."""
    validate_config(config)
    url = url or os.environ.get(
        "OLLAMA_RECOVERY_URL", "http://ollama.llama.svc:9111/preparation/recover"
    )
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/preparation/recover"
    ):
        raise PreparationError("invalid recovery endpoint")
    result = Http(
        urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    ).call(parsed.path, config, timeout=660)
    if (
        result.get("status") not in {"busy", "recovered"}
        or type(result.get("changed")) is not bool
    ):
        raise PreparationError("recovery endpoint returned no completed outcome")
    if result["changed"] != (result["status"] == "recovered"):
        raise PreparationError("inconsistent recovery outcome")
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(request_recovery(json.loads(os.environ["OLLAMA_RECOVERY"]))))
    except (PreparationError, ValueError, KeyError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
