#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["openviking"]
# ///

import subprocess
# Check if there's an HttpClient or RemoteClient
r = subprocess.run(['grep', '-rn', 'class.*Client', '/home/natew/.cache/uv/environments-v2/example-8fd551c34145242b/lib/python3.13/site-packages/openviking/client/'], capture_output=True, text=True)
print("Client classes:")
print(r.stdout)

# Check for ovcli config
r2 = subprocess.run(['grep', '-rn', 'base_url\|HttpClient\|RemoteClient\|ovcli', '/home/natew/.cache/uv/environments-v2/example-8fd551c34145242b/lib/python3.13/site-packages/openviking/client/'], capture_output=True, text=True)
print("HTTP/remote refs:")
print(r2.stdout[:1000])

# Check the ovcli client
r3 = subprocess.run(['grep', '-rn', 'class.*Client', '/home/natew/.cache/uv/environments-v2/example-8fd551c34145242b/lib/python3.13/site-packages/openviking_cli/'], capture_output=True, text=True)
print("CLI client classes:")
print(r3.stdout[:1000])
