#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["openviking"]
# ///
"""
OpenViking quickstart example.

Requires:
  OPENVIKING_URL  — default http://openviking.viking.svc:1933 (in-cluster);
                    use http://192.168.1.19:31933 on the LAN, or
                    https://context.nathanwhyte.dev from off-LAN/external.
  OPENVIKING_KEY  — API key for the OpenViking instance
"""

import os

from openviking_cli.client.sync_http import SyncHTTPClient

client = SyncHTTPClient(
    url=os.environ.get("OPENVIKING_URL", "http://openviking.viking.svc:1933"),
    api_key=os.environ["OPENVIKING_KEY"],
    timeout=300.0,  # 5 min — VLM processing of large docs takes time
)

try:
    client.initialize()
    print("Client initialized")

    add_result = client.add_resource(
        path="https://raw.githubusercontent.com/volcengine/OpenViking/refs/heads/main/README.md"
    )
    root_uri = add_result["root_uri"]
    print(f"Resource added: {root_uri}")

    ls_result = client.ls(root_uri)
    print(f"\nDirectory structure:")
    for item in ls_result:
        kind = "d" if item["isDir"] else "f"
        print(f"  [{kind}] {item['name']}")

    glob_result = client.glob(pattern="**/*.md", uri=root_uri)
    if glob_result["matches"]:
        first_file = glob_result["matches"][0]
        content = client.read(first_file)
        print(f"\nContent preview ({first_file}):\n{content[:200]}...")

    print("\nWaiting for semantic processing (VLM generates abstracts/overviews)...")
    result = client.wait_processed(timeout=240)
    for queue, stats in result.items():
        print(
            f"  {queue}: processed={stats['processed']}, errors={stats['error_count']}"
        )

    abstract = client.abstract(root_uri)
    overview = client.overview(root_uri)
    print(f"\nL0 Abstract:\n{abstract}")
    print(f"\nL1 Overview (first 500 chars):\n{overview[:500]}...")

    print("\nSemantic search: 'what is openviking'")
    results = client.find("what is openviking", target_uri=root_uri)
    print(f"\nSearch results:")
    for r in getattr(results, "resources", []) or []:
        uri = r.get("uri", r) if isinstance(r, dict) else getattr(r, "uri", r)
        score = r.get("score", "?") if isinstance(r, dict) else getattr(r, "score", "?")
        print(f"  - {uri} (score: {score})")

    client.close()
    print("\nDone")

except Exception as e:
    print(f"Error: {e}")
    import traceback

    traceback.print_exc()
