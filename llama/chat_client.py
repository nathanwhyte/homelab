#!/usr/bin/env python3
"""Minimal CLI client for llama-api with optional durable memory."""

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
DEFAULT_MEMORY_URL = "http://memory-service.llama.svc.cluster.local"
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
        "--memory",
        choices=("on", "off"),
        default=os.getenv("LLAMA_MEMORY", "on"),
        help="Enable durable memory retrieval/upsert (default: %(default)s)",
    )
    parser.add_argument(
        "--memory-base-url",
        default=os.getenv("LLAMA_MEMORY_BASE_URL", DEFAULT_MEMORY_URL),
        help="Memory service base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--user-id",
        default=os.getenv("LLAMA_USER_ID", "default-user"),
        help="Stable user identity for memory lookup (default: %(default)s)",
    )
    parser.add_argument(
        "--chat-id",
        default=os.getenv("LLAMA_CHAT_ID", ""),
        help="Chat session id (default: auto-generated)",
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


def post_json(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        response_body = resp.read().decode("utf-8")
    return json.loads(response_body) if response_body else {}


def call_llm(
    *,
    base_url: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        return post_json(f"{base_url.rstrip('/')}/chat/completions", payload, timeout)
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


def call_llm_with_kubectl(
    *,
    base_url: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: int,
    namespace: str,
    image: str,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
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
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for i, ch in enumerate(body):
            if ch != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(body[i:])
                return parsed
            except json.JSONDecodeError:
                continue
        raise SystemExit(f"Invalid JSON response from kubectl run. Raw: {body}")


def maybe_retrieve_memory(args: argparse.Namespace, prompt: str, chat_id: str) -> str:
    if args.memory != "on":
        return ""

    payload = {
        "user_id": args.user_id,
        "chat_id": chat_id,
        "query": prompt,
    }
    url = f"{args.memory_base_url.rstrip('/')}/memory/retrieve"

    try:
        result = post_json(url, payload, timeout=max(2, args.timeout // 2))
        return (result.get("memory_block") or "").strip()
    except Exception as exc:
        print(
            f"Warning: memory retrieval unavailable ({exc}). Continuing without memory.",
            file=sys.stderr,
        )
        return ""


def maybe_upsert_memory(
    args: argparse.Namespace,
    *,
    prompt: str,
    assistant_response: str,
    chat_id: str,
) -> None:
    if args.memory != "on":
        return

    payload = {
        "user_id": args.user_id,
        "chat_id": chat_id,
        "turns": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": assistant_response},
        ],
    }
    url = f"{args.memory_base_url.rstrip('/')}/memory/upsert-turn"

    try:
        post_json(url, payload, timeout=max(2, args.timeout // 2))
    except Exception as exc:
        print(f"Warning: memory upsert unavailable ({exc}).", file=sys.stderr)


def assistant_content(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def format_output(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return "No choices returned.\nRaw response:\n" + json.dumps(result, indent=2)

    content = assistant_content(result)
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
    chat_id = args.chat_id.strip() or f"chat-{uuid.uuid4().hex[:10]}"

    memory_block = maybe_retrieve_memory(args, prompt, chat_id)
    messages: list[dict] = []
    if memory_block:
        messages.append(
            {
                "role": "system",
                "content": (
                    "Use the memory context if it is relevant and consistent with the current request.\n"
                    "Memory context:\n"
                    f"{memory_block}"
                ),
            }
        )
    messages.append({"role": "user", "content": prompt})

    if args.kubectl_run:
        base_url = args.base_url
        if "127.0.0.1" in base_url or "localhost" in base_url:
            base_url = IN_CLUSTER_BASE_URL
        result = call_llm_with_kubectl(
            base_url=base_url,
            model=args.model,
            messages=messages,
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
            messages=messages,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            timeout=args.timeout,
        )

    print(format_output(result))
    maybe_upsert_memory(
        args,
        prompt=prompt,
        assistant_response=assistant_content(result),
        chat_id=chat_id,
    )


if __name__ == "__main__":
    main()
