"""Live BUG-1174 regression, run with stdin in the ov-test v0.4.20 pod.

Uses a unique prefix in openviking-agfs-test only. Two independent RAGFS clients
represent workers. First prime a negative stat; then create the directory in the
other client; finally acquire a lock beneath it. Cached must fail with the
reported error; uncached must succeed. No model calls or production writes.
The small synthetic prefix is retained for inspection.
"""

import json
import uuid

from openviking.pyagfs import RAGFSBindingClient
from openviking.utils.agfs_utils import _serialize_s3_plugin_params


def main():
    with open("/app/.openviking/ov.conf") as stream:
        config = json.load(stream)["storage"]["agfs"]["s3"]
    assert config["bucket"] == "openviking-agfs-test", "test bucket only"
    params = _serialize_s3_plugin_params(config)
    params["directory_marker_mode"] = "empty"
    params["prefix"] = "capture-validity-probe/" + uuid.uuid4().hex
    receipt = {"prefix": params["prefix"], "cases": []}
    for cached in (True, False):
        params["cache_enabled"] = cached
        clients = [
            RAGFSBindingClient(
                config={
                    "cache": {"enabled": False, "runtime_enabled": False},
                    "log": {"level": "ERROR", "output": "stdout"},
                }
            )
            for _ in range(2)
        ]
        for client in clients:
            client.mount("s3fs", "/local", params)
        reader, creator = clients
        parent = "/local/" + ("cached" if cached else "uncached")
        path = parent + "/history"
        try:
            reader.stat(path)
        except Exception as exc:
            assert type(exc).__name__ == "AGFSNotFoundError", str(exc)
        else:
            raise AssertionError("unique prefix was already present")
        creator.mkdir(parent)
        creator.mkdir(path)
        assert creator.stat(path)["isDir"]
        row = {"cache_enabled": cached}
        try:
            handle = reader.pathlock_acquire_exact({}, path + "/messages.jsonl")
        except Exception as exc:
            row["error"] = str(exc)
            assert (
                cached
                and "failed to create lock dir" in str(exc)
                and "already exists" in str(exc)
            ), str(exc)
        else:
            reader.pathlock_release({}, handle)
            assert not cached, "expected cached negative stat to reproduce the race"
            row["acquired_and_released"] = True
        receipt["cases"].append(row)
    receipt["passed"] = True
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
