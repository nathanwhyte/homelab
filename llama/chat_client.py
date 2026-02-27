#!/usr/bin/env python3
"""Minimal CLI client for llama-api with optional durable memory."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

try:
    from rich.console import Console  # type: ignore[import-not-found]
    from rich.markdown import Markdown  # type: ignore[import-not-found]
    from rich.table import Table  # type: ignore[import-not-found]

    RICH_AVAILABLE = True
except ImportError:
    Console = None
    Markdown = None
    Table = None
    RICH_AVAILABLE = False


DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
IN_CLUSTER_BASE_URL = "http://llama-api.llama.svc.cluster.local/v1"
DEFAULT_MEMORY_URL = "http://memory-service.llama.svc.cluster.local"
IN_CLUSTER_MEMORY_URL = "http://memory-service.llama.svc.cluster.local"
DEFAULT_MODEL = "current.gguf"

CONSOLE = Console() if RICH_AVAILABLE and Console is not None else None


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
    return post_json_with_kubectl(
        url=endpoint,
        payload=payload,
        timeout=timeout,
        namespace=namespace,
        image=image,
        pod_prefix="llama-chat",
    )


def post_json_with_kubectl(
    *,
    url: str,
    payload: dict,
    timeout: int,
    namespace: str,
    image: str,
    pod_prefix: str,
) -> dict:
    pod_name = f"{pod_prefix}-{uuid.uuid4().hex[:8]}"

    cmd = [
        "kubectl",
        "run",
        pod_name,
        "-n",
        namespace,
        "--rm",
        "-i",
        "--quiet",
        "--restart=Never",
        "--image",
        image,
        "--command",
        "--",
        "curl",
        "-sS",
        url,
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
        if args.kubectl_run:
            memory_url = url
            if "127.0.0.1" in memory_url or "localhost" in memory_url:
                memory_url = f"{IN_CLUSTER_MEMORY_URL}/memory/retrieve"
            result = post_json_with_kubectl(
                url=memory_url,
                payload=payload,
                timeout=max(2, args.timeout // 2),
                namespace=args.namespace,
                image=args.kubectl_image,
                pod_prefix="llama-memory-get",
            )
        else:
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
        if args.kubectl_run:
            memory_url = url
            if "127.0.0.1" in memory_url or "localhost" in memory_url:
                memory_url = f"{IN_CLUSTER_MEMORY_URL}/memory/upsert-turn"
            post_json_with_kubectl(
                url=memory_url,
                payload=payload,
                timeout=max(2, args.timeout // 2),
                namespace=args.namespace,
                image=args.kubectl_image,
                pod_prefix="llama-memory-put",
            )
        else:
            post_json(url, payload, timeout=max(2, args.timeout // 2))
    except Exception as exc:
        print(f"Warning: memory upsert unavailable ({exc}).", file=sys.stderr)


def assistant_content(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def render_output(result: dict) -> None:
    choices = result.get("choices") or []
    if not choices:
        if RICH_AVAILABLE and CONSOLE is not None:
            CONSOLE.rule("[bold]Assistant[/bold]")
            CONSOLE.print("No choices returned.")
            CONSOLE.print_json(data=result)
        else:
            print(
                "No choices returned.\nRaw response:\n" + json.dumps(result, indent=2)
            )
        return

    content = assistant_content(result)
    usage = result.get("usage") or {}

    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")

    if (
        RICH_AVAILABLE
        and CONSOLE is not None
        and Markdown is not None
        and Table is not None
    ):
        CONSOLE.rule("[bold]Assistant[/bold]")
        if content:
            CONSOLE.print(Markdown(content))
        else:
            CONSOLE.print("(empty response)")

        if any(v is not None for v in (prompt_tokens, completion_tokens, total_tokens)):
            usage_table = Table(title="Usage")
            usage_table.add_column("prompt_tokens", justify="right")
            usage_table.add_column("completion_tokens", justify="right")
            usage_table.add_column("total_tokens", justify="right")
            usage_table.add_row(
                str(prompt_tokens),
                str(completion_tokens),
                str(total_tokens),
            )
            CONSOLE.print(usage_table)
        return

    lines = ["Assistant", "---------"]
    if content:
        lines.append(content)
    else:
        lines.append("(empty response)")

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
    print("\n".join(lines))


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

    render_output(result)
    maybe_upsert_memory(
        args,
        prompt=prompt,
        assistant_response=assistant_content(result),
        chat_id=chat_id,
    )


if __name__ == "__main__":
    main()
