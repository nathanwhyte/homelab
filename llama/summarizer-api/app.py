#!/usr/bin/env python3
"""Agent context summarization API with OpenViking tool calling.

POST /summarize     - summarize context text
POST /v1/summarize  - alias
POST /v1/agent      - agentic tool-calling loop with OpenViking
GET  /healthz       - health check
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import urllib3

LLM_URL = os.getenv("LLM_URL", "http://qwen-summarizer-llm.llama.svc.cluster.local/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3-14b")
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("BIND_PORT", "8082"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "1024"))
MAX_LLM_CONCURRENT = int(os.getenv("MAX_LLM_CONCURRENT", "2"))
_llm_semaphore = threading.Semaphore(MAX_LLM_CONCURRENT)

SYSTEM_PROMPTS = {
    "context": (
        "/no_think You are a precise context summarizer for an AI agent system. "
        "Summarize the provided context into a concise, structured format that preserves: "
        "1) Key decisions and their rationale, "
        "2) Important facts, names, and values, "
        "3) Current state and pending actions, "
        "4) Any errors or blockers encountered. "
        "Use bullet points. Omit filler and pleasantries. Be terse."
    ),
    "conversation": (
        "/no_think You are a conversation summarizer. Condense the conversation into key points: "
        "what was discussed, what was decided, what actions were taken, and what remains open. "
        "Preserve technical details and specific values. Use bullet points."
    ),
    "code": (
        "/no_think You are a code change summarizer. Given code context, describe: "
        "1) What changed and why, "
        "2) Key files and functions affected, "
        "3) Any notable patterns or decisions. "
        "Be specific about names, paths, and values."
    ),
}


# --- OpenViking agent configuration ---
OV_URL = os.getenv("OPENVIKING_URL", "http://openviking.viking.svc.cluster.local:1933")
OV_KEY = os.getenv("OPENVIKING_KEY", "")
OV_ACCOUNT = os.getenv("OPENVIKING_ACCOUNT", "default")
OV_USER = os.getenv("OPENVIKING_USER", "noot")

OV_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {OV_KEY}",
    "X-OpenViking-Account": OV_ACCOUNT,
    "X-OpenViking-User": OV_USER,
}

# Connection pools — reuse TCP connections across requests
_llm_parsed = urllib.parse.urlparse(LLM_URL)
_ov_parsed = urllib.parse.urlparse(OV_URL)
_llm_pool = urllib3.HTTPConnectionPool(_llm_parsed.hostname, port=_llm_parsed.port or 80, maxsize=4, timeout=120, retries=False)
_ov_pool = urllib3.HTTPConnectionPool(_ov_parsed.hostname, port=_ov_parsed.port or 80, maxsize=4, timeout=30, retries=False)
_llm_path = _llm_parsed.path

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "viking_search",
            "description": "Semantic search across the OpenViking knowledge base. Returns ranked results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "scope": {"type": "string", "description": "URI scope to search within", "default": "viking://"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "viking_read",
            "description": "Read the full content of a file in the OpenViking filesystem by URI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Viking URI to read, e.g. viking://resources/..."},
                },
                "required": ["uri"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "viking_find",
            "description": "Directory-recursive semantic search. More targeted than viking_search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "target_uri": {"type": "string", "description": "Directory URI to search within", "default": "viking://"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "viking_ls",
            "description": "List contents of a directory in the OpenViking filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Directory URI to list", "default": "viking://"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "viking_add_text",
            "description": "Add text content to the OpenViking knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Text content to add"},
                    "name": {"type": "string", "description": "Name for the resource"},
                    "target_dir": {"type": "string", "description": "Target directory URI", "default": "viking://resources"},
                },
                "required": ["content", "name"],
            },
        },
    },
]

# (method, path, body_builder, use_params)
# use_params=True sends as URL query params (GET), False sends as JSON body (POST)
TOOL_DISPATCH = {
    "viking_search": ("POST", "/api/v1/search/search", lambda a: {"query": a["query"], "scope": a.get("scope", "viking://")}, False),
    "viking_read": ("GET", "/api/v1/content/read", lambda a: {"uri": a["uri"]}, True),
    "viking_find": ("POST", "/api/v1/search/find", lambda a: {"query": a["query"], "uri": a.get("target_uri", "viking://")}, False),
    "viking_ls": ("GET", "/api/v1/fs/ls", lambda a: {"uri": a.get("uri", "viking://")}, True),
    "viking_add_text": ("POST", "/api/v1/resources", lambda a: {"content": a["content"], "name": a["name"], "target_dir": a.get("target_dir", "viking://resources")}, False),
}


def ov_request(method: str, path: str, params: dict, use_params: bool = False) -> dict:
    """Make a request to the OpenViking API using connection pool."""
    if use_params:
        query = urllib.parse.urlencode(params)
        url = f"{path}?{query}"
    else:
        url = path
    try:
        body = None if use_params else json.dumps(params).encode("utf-8")
        resp = _ov_pool.urlopen(method, url, body=body, headers=OV_HEADERS)
        return json.loads(resp.data.decode("utf-8"))
    except urllib3.exceptions.HTTPError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute an OpenViking tool and return the result as a string."""
    if name not in TOOL_DISPATCH:
        return json.dumps({"error": f"Unknown tool: {name}"})
    method, path, build_body, use_params = TOOL_DISPATCH[name]
    result = ov_request(method, path, build_body(arguments), use_params)
    result_str = json.dumps(result)
    if len(result_str) > 4000:
        result_str = result_str[:4000] + "...(truncated)"
    return result_str


def llm_request_with_tools(messages: list[dict], max_tokens: int, temperature: float, tools: list[dict] | None = None) -> dict:
    """LLM request with optional tool definitions using connection pool."""
    with _llm_semaphore:
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "min_p": 0.05,
            "repeat_penalty": 1.1,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = json.dumps(payload).encode("utf-8")
        resp = _llm_pool.urlopen("POST", _llm_path, body=data, headers={"Content-Type": "application/json"})
        if resp.status >= 400:
            raise urllib3.exceptions.HTTPError(f"HTTP {resp.status}")
        return json.loads(resp.data.decode("utf-8"))


def extract_content(message: dict) -> str:
    """Extract text content from an LLM response message.

    Handles Qwen3's dual content fields: 'content' (normal) and
    'reasoning_content' (thinking). Also strips <think> and <tool_call> tags.
    """
    content = message.get("content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    content = re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL).strip()
    if not content:
        content = (message.get("reasoning_content") or "").strip()
    return content


def parse_text_tool_calls(content: str) -> list[dict]:
    """Parse Qwen3's text-based <tool_call> format into structured tool calls.

    Qwen3 sometimes emits tool calls as XML in content instead of using
    the structured tool_calls field. This detects and parses them.
    """
    tool_calls = []
    for match in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", content, re.DOTALL):
        try:
            tc = json.loads(match.group(1))
            tool_calls.append({
                "id": f"text_tc_{len(tool_calls)}",
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": json.dumps(tc.get("arguments", {})),
                },
            })
        except json.JSONDecodeError:
            continue
    return tool_calls


def agent_loop(messages: list[dict], max_tokens: int = 2048, temperature: float = 0.3, max_iterations: int = 10) -> dict:
    """Run the agentic tool-calling loop."""
    all_messages = list(messages)
    tools_used = []
    total_tokens = 0

    for i in range(max_iterations):
        try:
            result = llm_request_with_tools(all_messages, max_tokens, temperature, AGENT_TOOLS)
        except urllib3.exceptions.HTTPError as e:
            print(f"  [agent] LLM error {e} at iter={i+1}, returning partial result")
            last = extract_content(all_messages[-1]) if all_messages else ""
            return {
                "ok": True,
                "content": last or f"(Agent stopped: LLM returned {e} after {len(tools_used)} tool calls -- likely context overflow)",
                "tools_used": tools_used,
                "iterations": i + 1,
                "total_tokens": total_tokens,
                "partial": True,
            }
        total_tokens += result.get("usage", {}).get("total_tokens", 0)

        choice = result.get("choices", [{}])[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "")

        print(f"  [agent] iter={i+1} finish={finish_reason} has_tool_calls={bool(message.get('tool_calls'))} content_len={len(message.get('content') or '')} reasoning_len={len(message.get('reasoning_content') or '')}")

        all_messages.append(message)

        tool_calls = message.get("tool_calls")

        if not tool_calls:
            text_content = (message.get("content") or "") + (message.get("reasoning_content") or "")
            if "<tool_call>" in text_content:
                tool_calls = parse_text_tool_calls(text_content)
                print(f"  [agent] parsed {len(tool_calls)} text-based tool call(s)")

        if not tool_calls:
            content = extract_content(message)
            return {
                "ok": True,
                "content": content,
                "tools_used": tools_used,
                "iterations": i + 1,
                "total_tokens": total_tokens,
            }

        is_text_parsed = any(tc.get("id", "").startswith("text_tc_") for tc in tool_calls)
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                tool_args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}

            print(f"  [agent] tool call: {tool_name}({json.dumps(tool_args)[:200]})")
            tool_result = execute_tool(tool_name, tool_args)
            tools_used.append({"tool": tool_name, "args": tool_args})

            if is_text_parsed:
                all_messages.append({
                    "role": "user",
                    "content": f"[Tool result for {tool_name}]: {tool_result}",
                })
            else:
                all_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_result,
                })

    last_content = extract_content(all_messages[-1]) if all_messages else ""
    return {
        "ok": True,
        "content": last_content,
        "tools_used": tools_used,
        "iterations": max_iterations,
        "total_tokens": total_tokens,
        "truncated": True,
    }


def llm_request(messages: list[dict], max_tokens: int, temperature: float) -> dict:
    with _llm_semaphore:
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "min_p": 0.05,
            "repeat_penalty": 1.1,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        resp = _llm_pool.urlopen("POST", _llm_path, body=data, headers={"Content-Type": "application/json"})
        if resp.status >= 400:
            raise urllib3.exceptions.HTTPError(f"HTTP {resp.status}")
        result = json.loads(resp.data.decode("utf-8"))

        choices = result.get("choices", [])
        if not choices:
            raise ValueError("No choices in LLM response")

        content = choices[0].get("message", {}).get("content", "")
        timings = result.get("timings", {})
        usage = result.get("usage", {})
        return {"content": content, "timings": timings, "usage": usage}


class SummarizerHandler(BaseHTTPRequestHandler):
    server_version = "summarizer-api/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(fmt % args)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def parse_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path in ("/summarize", "/v1/summarize"):
            self.handle_summarize()
            return
        if self.path == "/v1/agent":
            self.handle_agent()
            return
        self.send_json(404, {"error": "not_found"})

    def handle_summarize(self) -> None:
        try:
            body = self.parse_json_body()
            context = (body.get("context") or "").strip()
            mode = (body.get("mode") or "context").strip()
            max_tokens = int(body.get("max_tokens") or DEFAULT_MAX_TOKENS)
            temperature = float(body.get("temperature") or 0.3)

            if not context:
                self.send_json(400, {"error": "context is required"})
                return

            system_prompt = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["context"])

            if body.get("system_prompt"):
                system_prompt = body["system_prompt"]

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context},
            ]

            result = llm_request(messages, max_tokens, temperature)
            summary = result["content"]
            timings = result["timings"]
            usage = result["usage"]

            response = {
                "ok": True,
                "summary": summary,
                "mode": mode,
                "input_chars": len(context),
                "output_chars": len(summary),
                "usage": usage,
            }
            if timings:
                response["perf"] = {
                    "prompt_tok_s": round(timings.get("prompt_per_second", 0), 1),
                    "gen_tok_s": round(timings.get("predicted_per_second", 0), 1),
                    "prompt_ms": round(timings.get("prompt_ms", 0), 1),
                    "gen_ms": round(timings.get("predicted_ms", 0), 1),
                }
            self.send_json(200, response)

        except Exception as exc:
            self.send_json(500, {"error": "summarize_failed", "detail": str(exc)})

    def handle_agent(self) -> None:
        try:
            body = self.parse_json_body()
            messages = body.get("messages")
            if not messages:
                self.send_json(400, {"error": "messages is required"})
                return

            max_tokens = int(body.get("max_tokens") or 2048)
            temperature = float(body.get("temperature") or 0.3)
            max_iterations = int(body.get("max_iterations") or 10)
            system_prompt = body.get("system_prompt")

            if system_prompt:
                messages = [{"role": "system", "content": system_prompt}] + messages
            elif not any(m.get("role") == "system" for m in messages):
                messages = [{
                    "role": "system",
                    "content": (
                        "/no_think You are a helpful assistant with access to the OpenViking knowledge base. "
                        "Use the provided tools to search, read, and store information. "
                        "Be concise and precise in your responses."
                    ),
                }] + messages

            print(f"[agent] Starting loop: {len(messages)} messages, max_iter={max_iterations}")
            result = agent_loop(messages, max_tokens, temperature, max_iterations)
            print(f"[agent] Done: {result.get('iterations')} iterations, {len(result.get('tools_used', []))} tool calls")
            self.send_json(200, result)

        except Exception as exc:
            self.send_json(500, {"error": "agent_failed", "detail": str(exc)})


def main() -> None:
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), SummarizerHandler)
    print(f"summarizer-api listening on {BIND_HOST}:{BIND_PORT}")
    print(f"LLM backend: {LLM_URL}")
    print(f"Modes: {', '.join(SYSTEM_PROMPTS.keys())}")
    print(f"Agent endpoint: /v1/agent (OpenViking: {OV_URL})")
    print(f"Agent tools: {', '.join(t['function']['name'] for t in AGENT_TOOLS)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
