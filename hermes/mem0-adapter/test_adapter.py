import os
import subprocess
import sys
import time

import httpx

# Start a tiny mock OSS server on port 28080
mock_code = """
import uvicorn
from fastapi import FastAPI
app = FastAPI()

@app.get('/')
def root():
    return {'message': 'oss root'}

@app.post('/memories')
def add(body: dict):
    return {'results': [{'id': 'm1', 'memory': body['messages'][0]['content']}]}

@app.post('/search')
def search(body: dict):
    return {'results': [{'id': 'm1', 'memory': 'found: ' + body['query']}]}

@app.get('/memories')
def get_all(user_id: str = None, agent_id: str = None):
    return [{'id': 'm1', 'memory': f'user={user_id} agent={agent_id}'}]

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=28080, log_level='warning')
"""

proc = subprocess.Popen(
    [sys.executable, "-c", mock_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
time.sleep(2)

# Start adapter pointing at mock
adapter_env = {
    **dict(os.environ),
    "MEM0_URL": "http://127.0.0.1:28080",
    "ADMIN_API_KEY": "fallback-key",
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
    r = httpx.get(
        "http://127.0.0.1:18080/v1/ping/", headers={"Authorization": "Token test-key"}
    )
    print("ping", r.status_code, r.json())

    r = httpx.post(
        "http://127.0.0.1:18080/v3/memories/add/",
        headers={"Authorization": "Token test-key", "Content-Type": "application/json"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    print("add", r.status_code, r.json())

    r = httpx.post(
        "http://127.0.0.1:18080/v3/memories/search/",
        headers={"Authorization": "Token test-key", "Content-Type": "application/json"},
        json={"query": "hello"},
    )
    print("search", r.status_code, r.json())

    r = httpx.post(
        "http://127.0.0.1:18080/v3/memories/",
        headers={"Authorization": "Token test-key", "Content-Type": "application/json"},
        json={"filters": {"user_id": "u1"}, "page": 1},
    )
    print("get_all", r.status_code, r.text)
finally:
    adapter_proc.terminate()
    proc.terminate()
    adapter_proc.wait(timeout=5)
    proc.wait(timeout=5)
    print("--- adapter logs ---")
    print(adapter_proc.stdout.read())
