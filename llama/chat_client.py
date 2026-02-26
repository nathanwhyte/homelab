#!/usr/bin/env python3
"""Minimal CLI client for the in-cluster llama OpenAI endpoint."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
import uuid


DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
IN_CLUSTER_BASE_URL = "http://llama-api.llama.svc.cluster.local/v1"
DEFAULT_MODEL = "current.gguf"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one prompt to llama-api and print formatted output."
    )
    parser.add_argument("prompt", nargs="?", help="Prompt text to send")
    parser.add_argument(
        "--base-url",
        default=os.getenv("LLAMA_BASE_URL", DEFAULT_BASE_URL),
        help="OpenAI-compatible base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LLAMA_MODEL", DEFAULT_MODEL),
        help="Model name to request (default: %(default)s)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="Maximum generated tokens (default: %(default)s)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--kubectl-run",
        action="store_true",
        help="Run the request from an ephemeral in-cluster pod via kubectl run",
    )
    parser.add_argument(
        "--namespace",
        default=os.getenv("LLAMA_NAMESPACE", "llama"),
        help="Namespace for kubectl run pod (default: %(default)s)",
    )
    parser.add_argument(
        "--kubectl-image",
        default=os.getenv("LLAMA_KUBECTL_IMAGE", "curlimages/curl:8.12.1"),
        help="Container image used for kubectl run mode (default: %(default)s)",
    )
    return parser.parse_args()


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt.strip()

    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            return data

    raise SystemExit("No prompt provided. Pass text as an argument or pipe via stdin.")


def call_llm(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        details = err.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {err.code}: {details}") from err
    except urllib.error.URLError as err:
        reason = str(err.reason)
        if "svc.cluster.local" in base_url:
            raise SystemExit(
                "Connection error: "
                f"{reason}\n\n"
                "The in-cluster service DNS is not resolvable from your local shell.\n"
                "Run this in another terminal:\n"
                "  kubectl -n llama port-forward svc/llama-api 8000:80\n"
                "Then rerun with:\n"
                '  python3 llama/chat_client.py "your prompt" --base-url http://127.0.0.1:8000/v1'
            ) from err
        raise SystemExit(
            "Connection error: "
            f"{reason}\n\n"
            "If running from your laptop, port-forward first:\n"
            "  kubectl -n llama port-forward svc/llama-api 8000:80"
        ) from err

    try:
        return json.loads(response_body)
    except json.JSONDecodeError as err:
        raise SystemExit(f"Invalid JSON response: {err}") from err


def call_llm_with_kubectl(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout: int,
    namespace: str,
    image: str,
) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    pod_name = f"llama-chat-{uuid.uuid4().hex[:8]}"

    cmd = [
        "kubectl",
        "run",
        pod_name,
        "-n",
        namespace,
        "--rm",
        "-i",
        "--restart=Never",
        "--image",
        image,
        "--command",
        "--",
        "curl",
        "-sS",
        endpoint,
        "-H",
        "Content-Type: application/json",
        "-d",
        json.dumps(payload),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    except FileNotFoundError as err:
        raise SystemExit("kubectl not found in PATH.") from err
    except subprocess.TimeoutExpired as err:
        raise SystemExit("kubectl run request timed out.") from err

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise SystemExit(f"kubectl run failed: {detail}")

    body = (proc.stdout or "").strip()
    if not body:
        raise SystemExit("Empty response from kubectl run request.")

    try:
        return json.loads(body)
    except json.JSONDecodeError as err:
        decoder = json.JSONDecoder()
        for i, ch in enumerate(body):
            if ch != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(body[i:])
                return parsed
            except json.JSONDecodeError:
                continue
        raise SystemExit(
            f"Invalid JSON response from kubectl run: {err}\nRaw: {body}"
        ) from err


def format_output(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return "No choices returned.\nRaw response:\n" + json.dumps(result, indent=2)

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    usage = result.get("usage") or {}

    lines = ["Assistant", "---------"]
    if content:
        lines.append(textwrap.fill(content, width=100))
    else:
        lines.append("(empty response)")

    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")

    if any(v is not None for v in (prompt_tokens, completion_tokens, total_tokens)):
        lines.extend(
            [
                "",
                "Usage",
                "-----",
                f"prompt_tokens: {prompt_tokens}",
                f"completion_tokens: {completion_tokens}",
                f"total_tokens: {total_tokens}",
            ]
        )

    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    prompt = read_prompt(args)
    if args.kubectl_run:
        base_url = args.base_url
        if "127.0.0.1" in base_url or "localhost" in base_url:
            base_url = IN_CLUSTER_BASE_URL
        result = call_llm_with_kubectl(
            base_url=base_url,
            model=args.model,
            prompt=prompt,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            timeout=args.timeout,
            namespace=args.namespace,
            image=args.kubectl_image,
        )
    else:
        result = call_llm(
            base_url=args.base_url,
            model=args.model,
            prompt=prompt,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            timeout=args.timeout,
        )
    print(format_output(result))


if __name__ == "__main__":
    main()
