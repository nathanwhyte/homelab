#!/usr/bin/env python3
"""OV Database Consolidation + Correctness Check.

Phase 1: Merge all worker content to worker-0 by reindexing missing URIs.
Phase 2: Compare federated search (coordinator) vs worker-0 only search.
"""

import os

import httpx

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

COORDINATOR_URL = os.environ.get(
    "OV_COORDINATOR_URL",
    "http://openviking.viking.svc.cluster.local:1933",
)
WORKER_URLS = os.environ.get(
    "OV_WORKERS",
    "http://ov-worker-0.ov-worker-headless.viking.svc.cluster.local:1933,"
    "http://ov-worker-1.ov-worker-headless.viking.svc.cluster.local:1933,"
    "http://ov-worker-2.ov-worker-headless.viking.svc.cluster.local:1933",
).split(",")
API_KEY = os.environ.get("OV_API_KEY", "")

PRIMARY_WORKER = WORKER_URLS[0]  # worker-0 is the consolidation target

HEADERS = {
    "X-API-Key": API_KEY,
    "X-OpenViking-Account": "default",
    "X-OpenViking-User": "noot",
    "Content-Type": "application/json",
}

TEST_QUERIES = [
    "GPU fan control thermal management",
    "llamacpp ROCm deployment kubernetes",
    "openviking configmap embedding",
    "Garage S3 storage Longhorn",
    "flannel network subnet configuration",
]

TIMEOUT = httpx.Timeout(120.0, connect=30.0)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=HEADERS, timeout=TIMEOUT)


def _result(resp: httpx.Response):
    """Extract result from OV API response, raising on error."""
    data = resp.json()
    if data.get("status") == "error":
        err = data.get("error", {})
        raise RuntimeError(f"OV error: {err.get('message', data)}")
    return data.get("result")


# ---------------------------------------------------------------------------
# Phase 1: Consolidation
# ---------------------------------------------------------------------------


def list_uris(worker_url: str) -> set[str]:
    """List all non-directory URIs on a worker."""
    with _client(worker_url) as c:
        resp = c.get("/api/v1/fs/tree", params={"uri": "viking://", "level_limit": 99})
        tree = _result(resp)

    uris: set[str] = set()
    if not tree:
        return uris
    for node in tree:
        if not node.get("isDir", False):
            uris.add(node["uri"])
    return uris


def consolidate(primary: str, workers: list[str]):
    """Phase 1: Reindex missing URIs on the primary worker."""
    print(f"Listing URIs on primary ({primary}) ...")
    primary_uris = list_uris(primary)
    print(f"  Primary has {len(primary_uris)} URIs")

    # Collect URIs from all other workers
    other_uris: set[str] = set()
    for w in workers:
        if w == primary:
            continue
        print(f"Listing URIs on {w} ...")
        w_uris = list_uris(w)
        print(f"  {w} has {len(w_uris)} URIs")
        other_uris |= w_uris

    missing = other_uris - primary_uris
    already = other_uris & primary_uris

    if not missing:
        print(f"Consolidated 0 URIs to worker-0 ({len(already)} already present, 0 new)")
        return

    print(f"Need to reindex {len(missing)} URIs on worker-0 ...")

    with _client(primary) as c:
        for i, uri in enumerate(sorted(missing), 1):
            try:
                resp = c.post(
                    "/api/v1/content/reindex",
                    json={"uri": uri, "regenerate": True, "wait": True},
                )
                _result(resp)
                print(f"[consolidate] Reindexed {uri} on worker-0 ({i}/{len(missing)})")
            except Exception as exc:
                print(f"[consolidate] FAILED {uri}: {exc} ({i}/{len(missing)})")

    print(
        f"Consolidated {len(missing)} URIs to worker-0 "
        f"({len(already)} already present, {len(missing)} new)"
    )


# ---------------------------------------------------------------------------
# Phase 2: Correctness Check
# ---------------------------------------------------------------------------


def search(base_url: str, query: str) -> list[str]:
    """Run a search query and return list of result URIs."""
    with _client(base_url) as c:
        resp = c.post("/api/v1/search/search", json={"query": query})
        result = _result(resp)

    uris: list[str] = []
    for section in ("resources", "memories", "skills"):
        for item in result.get(section, []):
            uris.append(item["uri"])
    return uris


def correctness_check(coordinator: str, primary: str, queries: list[str]):
    """Phase 2: Compare federated vs primary-only search results."""
    full_match = 0
    total_missing = 0

    for query in queries:
        fed_uris = search(coordinator, query)
        pri_uris = search(primary, query)

        fed_set = set(fed_uris)
        pri_set = set(pri_uris)
        missing = fed_set - pri_set

        match_pct = (
            round((len(fed_set) - len(missing)) / len(fed_set) * 100)
            if fed_set
            else 100
        )

        print(f'Query: "{query}"')
        print(f"  Federated: {len(fed_uris)} results {fed_uris}")
        print(f"  Worker-0:  {len(pri_uris)} results {pri_uris}")
        if missing:
            print(f"  Missing from worker-0: {sorted(missing)}")
        print(f"  Match: {match_pct}%")
        print()

        if not missing:
            full_match += 1
        total_missing += len(missing)

    print(
        f"Correctness: {full_match}/{len(queries)} queries fully matched, "
        f"{total_missing} URIs missing from worker-0"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== OV Consolidation Job ===")
    print(f"Primary: {PRIMARY_WORKER}")
    print(f"Workers: {WORKER_URLS}")
    print(f"Coordinator: {COORDINATOR_URL}")

    print("\n--- Phase 1: Consolidation ---")
    consolidate(PRIMARY_WORKER, WORKER_URLS)

    print("\n--- Phase 2: Correctness Check ---")
    correctness_check(COORDINATOR_URL, PRIMARY_WORKER, TEST_QUERIES)


if __name__ == "__main__":
    main()
