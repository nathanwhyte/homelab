import os
import subprocess
import sys
import time

from mem0 import MemoryClient

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
if not ADMIN_API_KEY:
    raise RuntimeError("Set ADMIN_API_KEY env var")

adapter_env = {
    **dict(os.environ),
    "MEM0_URL": "http://127.0.0.1:28080",
    "ADMIN_API_KEY": ADMIN_API_KEY,
}
adapter_proc = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "18080",
        "--log-level",
        "info",
    ],
    env=adapter_env,
    cwd=os.path.dirname(os.path.abspath(__file__)),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)
time.sleep(2)

try:
    client = MemoryClient(api_key=ADMIN_API_KEY, host="http://127.0.0.1:18080")
    user_id = f"test-user-{int(time.time())}"

    add_resp = client.add(
        [{"role": "user", "content": "I prefer dark mode for coding"}],
        user_id=user_id,
    )
    print("add:", add_resp)

    search_resp = client.search("dark mode", filters={"user_id": user_id})
    print("search:", search_resp)

    all_resp = client.get_all(filters={"user_id": user_id})
    print("get_all:", all_resp)
finally:
    adapter_proc.terminate()
    adapter_proc.wait(timeout=5)
    print("--- adapter logs ---")
    print(adapter_proc.stdout.read())
