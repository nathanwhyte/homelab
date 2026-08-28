#!/usr/bin/env python3
"""Partner-model gate for the compendium batch agent (IDEA-1090 / IDEA-1091).

Feeds a candidate model the REAL vault-resident batch skills (the SKILL.md
text from `~/code/compendium` branch `idea-batch-agent`) as its system prompt
and real vault items as input, framework-free, and scores the model's core
judgment with the vault's own rules:

  summary   retrieval_summary repair — corrupt a real compliant summary in one
            of three schema-breaking ways (over-length / double-quoted /
            colon-space), ask the model to repair it per the
            skill's edit rule, score: IMPR-1105 schema, every ID/PR/date/
            version/backtick token of the current summary preserved, and
            `_scripts/fact-token-verify.py` finds nothing hallucinated.
  fence     bare-fence (MD040) repair — take real tagged fences, strip the tag,
            ask for the tag per the skill's table, score exact class match
            (bash/yaml/json/python/sql/text).

Subcommands:
  build  [--vault DIR] [--out DIR] [--n-summary N] [--n-fence N]
         builds cases.json from the live vault (build-lane-records.py for
         records; rg for fences) — deterministic (seeded) so every model sees
         the same cases.
  run    --model TAG [--out DIR] [--host URL] [--think] [--temperature T]
         runs both case sets against one model on /api/chat with `format`
         JSON schema, think:false by default, temperature 0, num_ctx 16384;
         writes <out>/<model>.json and prints the score line.
  report [--out DIR]   prints the cross-model table from the per-model json.

One local-model consumer at a time: run models sequentially. Loading a
candidate evicts the pinned FIM runner (MAX_LOADED_MODELS=1); `run` re-warms
`deepseek-coder-v2:fim` (load-only, keep_alive -1) when it finishes unless
--no-rewarm is given.
"""

import argparse
import glob
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_VAULT = Path.home() / "code" / "compendium"
DEFAULT_OUT = (
    Path(__file__).resolve().parents[2] / "results" / "batch-skill-gate-20260828"
)
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://192.168.1.19:11434").rstrip("/")
FIM_MODEL = "deepseek-coder-v2:fim"
SKILL_BRANCH = "idea-batch-agent"
SKILL_DIR = ".claude/skills"

FENCE_CLASSES = {
    "bash": "bash",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "yaml": "yaml",
    "yml": "yaml",
    "json": "json",
    "jsonc": "json",
    "jsonl": "json",
    "python": "python",
    "py": "python",
    "sql": "sql",
    "text": "text",
    "txt": "text",
    "md": "text",
    "markdown": "text",
}
FENCE_TAGS = ["bash", "yaml", "json", "python", "sql", "text"]

TOKEN_RES = {
    "id": re.compile(r"\b[A-Z]{3,5}-\d{1,4}\b"),
    "pr": re.compile(r"\bPR\s*#?\s*\d+\b"),
    "date": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "version": re.compile(r"\b[vV]?\d+\.\d+(?:\.\d+)?[a-zA-Z0-9_-]*\b"),
    "backtick": re.compile(r"`[^`]+`"),
}


# ----------------------------------------------------------------------------- helpers
def git_show(vault: Path, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(vault), "show", f"{SKILL_BRANCH}:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def skill_commit(vault: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(vault), "rev-parse", "--short", SKILL_BRANCH],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def summary_violations(s: str) -> list[str]:
    v = []
    if not isinstance(s, str) or not s.strip():
        return ["empty"]
    if "\n" in s:
        v.append("multiline")
    if len(s) > 200:
        v.append(f"len={len(s)}")
    if '"' in s:
        v.append("double-quote")
    if ": " in s:
        v.append("colon-space")
    if " #" in s:
        v.append("space-hash")
    if s[0].isdigit():
        v.append("leading-digit")
    if s.lstrip().startswith((">-", "|")):
        v.append("block-scalar")
    return v


def tokens(s: str) -> set[str]:
    out = set()
    for kind, rx in TOKEN_RES.items():
        for m in rx.findall(s or ""):
            out.add(f"{kind}:{m}")
    return out


def corrupt(summary: str, mode: str) -> str:
    if mode == "overlength":
        filler = (
            " - see the dated notes in the body for the full investigation history,"
            " the follow-up work that was recorded afterwards, and the related"
            " entries that carry the surrounding context for this item"
        )
        s = summary
        while len(s) <= 200:
            s += filler
        return s
    if mode == "quoted":
        return f'"{summary}"'
    if mode == "colon-space":
        if " - " in summary:
            return summary.replace(" - ", ": ", 1)
        return summary + ": details in body"
    raise ValueError(mode)


# ----------------------------------------------------------------------------- build
def cmd_build(a):
    vault = Path(a.vault)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(20260828)
    # leading-digit was dropped 2026-08-28: the corruption prepends "2 items - ",
    # which pits the schema's "no leading digit" rule against "keep every fact".
    # Measured 0/5 for every candidate on the corrected count-preserving oracle
    # (batch-skill-gate-20260828-postfix2) — models either keep the digit or drop
    # the count, and a skill-text fix telling them to reorder or spell it out
    # changed nothing. It measures a model-capability gap, not batch-repair skill.
    modes = ["overlength", "quoted", "colon-space"]

    if a.from_cases:
        src = json.load(open(a.from_cases))
        summary_cases = src["summary_cases"]
        fence_cases = src["fence_cases"]
        # Pinned files built before 2026-08-28 still carry leading-digit cases;
        # drop them so a --from-cases replay matches a fresh build.
        dropped = [c for c in summary_cases if c["mode"] not in modes]
        if dropped:
            summary_cases = [c for c in summary_cases if c["mode"] in modes]
            print(
                f"dropped {len(dropped)} retired-mode case(s) from {a.from_cases}: "
                f"{sorted({c['mode'] for c in dropped})}"
            )
    else:
        # --- summary cases from real records (build-lane-records.py, the skill's ground-truth command)
        records = []
        for type_dir in ["bugs", "improvements", "ideas", "info"]:
            tmp = out / f"records-{type_dir}.json"
            subprocess.run(
                [
                    "uv",
                    "run",
                    "python",
                    "_scripts/build-lane-records.py",
                    "--type-dir",
                    type_dir,
                    "--output",
                    str(tmp),
                ],
                cwd=vault,
                check=True,
                capture_output=True,
                text=True,
            )
            d = json.load(open(tmp))
            recs = d.get("records", d) if isinstance(d, dict) else d
            records += [
                r
                for r in recs
                if r.get("summary") and not summary_violations(r["summary"])
            ]
            tmp.unlink()
        rnd.shuffle(records)
        summary_cases = []
        for i, r in enumerate(records[: a.n_summary]):
            mode = modes[i % len(modes)]
            current = corrupt(r["summary"], mode)
            summary_cases.append(
                {
                    "id": r["id"],
                    "path": r["path"],
                    "title": r["title"],
                    "mode": mode,
                    "original_summary": r["summary"],
                    "current_summary": current,
                    "required_tokens": sorted(tokens(current)),
                    "body_excerpt": r["body_excerpt"][: a.excerpt_chars],
                }
            )

        # --- fence cases from real tagged fences
        fence_cases = []
        per_class = {c: [] for c in FENCE_TAGS}
        files = [
            p
            for d in [
                "bugs",
                "info",
                "ideas",
                "improvements",
                "tasks",
                "guides",
                "errors",
            ]
            for p in glob.glob(str(vault / d / "**" / "*.md"), recursive=True)
        ]
        rnd.shuffle(files)
        fence_rx = re.compile(r"^```([a-zA-Z0-9_+-]+)\s*\n(.*?)^```\s*$", re.S | re.M)
        for p in files:
            try:
                text = open(p, encoding="utf-8").read()
            except Exception:
                continue
            for m in fence_rx.finditer(text):
                lang = m.group(1).lower()
                if lang not in FENCE_CLASSES:
                    continue
                cls = FENCE_CLASSES[lang]
                body = m.group(2).strip("\n")
                lines = body.splitlines()
                if len(lines) < 2 or len(body) < 40:
                    continue
                per_class[cls].append(
                    {
                        "path": os.path.relpath(p, vault),
                        "orig_tag": lang,
                        "label": cls,
                        "content": "\n".join(lines[:30]),
                    }
                )
        per = max(1, a.n_fence // len(FENCE_TAGS))
        for cls in FENCE_TAGS:
            pool = per_class[cls]
            rnd.shuffle(pool)
            fence_cases += pool[:per]

    # --- skill texts (system prompts), verbatim from the branch
    skills = {
        "contract": git_show(vault, f"{SKILL_DIR}/compendium-batch/SKILL.md"),
        "summary": git_show(
            vault, f"{SKILL_DIR}/compendium-batch-summary-repair/SKILL.md"
        ),
        "fence": git_show(vault, f"{SKILL_DIR}/compendium-batch-fence-repair/SKILL.md"),
    }
    sc = skill_commit(vault)
    cases = {
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "vault": str(vault),
        "skill_branch": SKILL_BRANCH,
        "skill_commit": sc,
        "skills": skills,
        "summary_cases": summary_cases,
        "fence_cases": fence_cases,
    }
    json.dump(cases, open(out / "cases.json", "w"), indent=1)
    print(
        f"built {len(summary_cases)} summary cases "
        f"({', '.join(f'{m}={sum(1 for c in summary_cases if c['mode'] == m)}' for m in modes)}) "
        f"and {len(fence_cases)} fence cases "
        f"({', '.join(f'{c}={sum(1 for f in fence_cases if f['label'] == c)}' for c in FENCE_TAGS)}) "
        f"skill {sc} -> {out / 'cases.json'}"
    )


# ----------------------------------------------------------------------------- run
def chat(host, model, system, user, schema, think, temperature, num_ctx, timeout=600):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": think,
        "format": schema,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": 1024,
        },
        "keep_alive": "10m",
    }
    req = urllib.request.Request(
        host + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    r["_wall"] = time.perf_counter() - t0
    return r


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "proposed_summary": {"type": ["string", "null"], "maxLength": 200},
        "reason": {"type": "string"},
    },
    "required": ["id", "proposed_summary", "reason"],
}
FENCE_SCHEMA = {
    "type": "object",
    "properties": {"tag": {"type": "string", "enum": FENCE_TAGS}},
    "required": ["tag"],
}

SUMMARY_USER = """You are repairing ONE entry's `retrieval_summary` under the skill above. Ground truth follows. Reply with JSON only: {{"id": "<id>", "proposed_summary": "<new single-line summary>" | null, "reason": "<one line>"}}. Use null for proposed_summary when the skill says to report instead of edit.

Record:
- id: {id}
- title: {title}
- current_summary (the value to repair, copy its facts exactly): {current}

body_excerpt:
{body}
"""

FENCE_USER = """Choose the language tag for this bare opening code fence per the table in the skill above. Reply with JSON only: {{"tag": "<one of bash|yaml|json|python|sql|text>"}}.

Fence content:
```
{content}
```
"""


def rewarm_fim(host):
    body = {"model": FIM_MODEL, "keep_alive": -1}
    req = urllib.request.Request(
        host + "/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=300).read())
        print("re-warmed", FIM_MODEL, r.get("done_reason"))
    except Exception as e:  # noqa: BLE001
        print("re-warm failed:", e)


def fact_token_verify(vault, proposals):
    tmp = Path("/tmp") / f"gate-proposals-{os.getpid()}.json"
    json.dump(proposals, open(tmp, "w"))
    r = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "_scripts/fact-token-verify.py",
            "--from-json",
            str(tmp),
            "--json",
        ],
        cwd=vault,
        capture_output=True,
        text=True,
    )
    tmp.unlink(missing_ok=True)
    try:
        flagged = (
            json.loads(r.stdout) if r.stdout.strip().startswith(("[", "{")) else []
        )
    except json.JSONDecodeError:
        flagged = []
    return flagged, (r.stdout + r.stderr)[-800:]


def majority(verdicts: list[str]) -> str:
    order = {"fail": 0, "report": 1, "pass": 2}
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts.values())
    top = [v for v, n in counts.items() if n == best]
    return min(top, key=lambda v: order[v])  # ties -> worse outcome


def cmd_run(a):
    out = Path(a.out)
    cases = json.load(open(out / "cases.json"))
    vault = Path(cases["vault"])
    host = a.host
    model = a.model
    ver = json.loads(urllib.request.urlopen(host + "/api/version", timeout=10).read())[
        "version"
    ]
    print(f"model={model} ollama={ver} think={a.think} temperature={a.temperature}")

    sys_summary = (
        cases["skills"]["contract"] + "\n\n---\n\n" + cases["skills"]["summary"]
    )
    sys_fence = cases["skills"]["contract"] + "\n\n---\n\n" + cases["skills"]["fence"]

    # warm / load timing
    t0 = time.perf_counter()
    chat(host, model, "reply with {}", "{}", {"type": "object"}, a.think, 0, a.num_ctx)
    load_s = time.perf_counter() - t0

    results = {
        "model": model,
        "ollama": ver,
        "think": a.think,
        "temperature": a.temperature,
        "num_ctx": a.num_ctx,
        "repeats": a.repeats,
        "load_s": load_s,
        "summary": [],
        "fence": [],
    }

    proposals = []
    for c in cases["summary_cases"]:
        user = SUMMARY_USER.format(
            id=c["id"],
            title=c["title"],
            current=c["current_summary"],
            body=c["body_excerpt"],
        )
        row = {"id": c["id"], "mode": c["mode"], "attempts": []}
        for attempt_idx in range(a.repeats):
            attempt = {}
            try:
                r = chat(
                    host,
                    model,
                    sys_summary,
                    user,
                    SUMMARY_SCHEMA,
                    a.think,
                    a.temperature,
                    a.num_ctx,
                )
                content = r["message"]["content"]
                attempt.update(
                    wall=r["_wall"],
                    prompt_tokens=r.get("prompt_eval_count"),
                    eval_tokens=r.get("eval_count"),
                    decode_tps=(r.get("eval_count") or 0)
                    / max(r.get("eval_duration", 1), 1)
                    * 1e9,
                    thinking_chars=len(r["message"].get("thinking") or ""),
                    raw=content[:600],
                )
                obj = json.loads(content)
                prop = obj.get("proposed_summary")
                attempt["json_ok"] = True
                attempt["proposed"] = prop
                attempt["reason"] = str(obj.get("reason", ""))[:200]
                if prop is None:
                    attempt["verdict"] = "report"
                else:
                    viol = summary_violations(prop)
                    missing = sorted(set(c["required_tokens"]) - tokens(prop))
                    attempt["violations"] = viol
                    attempt["missing_tokens"] = missing
                    attempt["verdict"] = "pass" if not viol and not missing else "fail"
                    proposals.append(
                        {
                            "id": f"{c['id']}#{attempt_idx}",
                            "path": c["path"],
                            "current_summary": c["current_summary"],
                            "proposed_summary": prop,
                        }
                    )
            except json.JSONDecodeError:
                attempt.update(
                    json_ok=False, verdict="fail", violations=["invalid-json"]
                )
            except Exception as e:  # noqa: BLE001
                attempt.update(json_ok=False, verdict="error", error=str(e)[:200])
            row["attempts"].append(attempt)
        results["summary"].append(row)

    flagged, ftv_out = fact_token_verify(vault, proposals) if proposals else ([], "")
    flagged_ids = {f.get("id") for f in flagged if isinstance(f, dict)}
    for row in results["summary"]:
        for attempt_idx, attempt in enumerate(row["attempts"]):
            if (
                attempt.get("verdict") == "pass"
                and f"{row['id']}#{attempt_idx}" in flagged_ids
            ):
                attempt["verdict"] = "fail"
                attempt.setdefault("violations", []).append("fact-token-verify")
        verdicts = [
            "fail" if a.get("verdict") == "error" else a.get("verdict", "error")
            for a in row["attempts"]
        ]
        row["verdict"] = majority(verdicts)
        for attempt in row["attempts"]:
            canon = (
                "fail" if attempt.get("verdict") == "error" else attempt.get("verdict")
            )
            if canon == row["verdict"]:
                for k in (
                    "violations",
                    "missing_tokens",
                    "proposed",
                    "reason",
                    "wall",
                    "prompt_tokens",
                    "eval_tokens",
                    "decode_tps",
                    "thinking_chars",
                    "raw",
                    "json_ok",
                    "error",
                ):
                    if k in attempt:
                        row[k] = attempt[k]
                break
        row["attempt_details"] = row.pop("attempts")
        print(
            f"  summary {row['id']:10s} {row['mode']:14s} -> {row['verdict']:6s} {row.get('violations', '')} {row.get('missing_tokens', '')} {row.get('wall', 0):.1f}s"
        )
    results["fact_token_verify_output"] = ftv_out

    for c in cases["fence_cases"]:
        user = FENCE_USER.format(content=c["content"])
        row = {
            "label": c["label"],
            "orig_tag": c["orig_tag"],
            "path": c["path"],
            "attempts": [],
        }
        for _ in range(a.repeats):
            attempt = {}
            try:
                r = chat(
                    host,
                    model,
                    sys_fence,
                    user,
                    FENCE_SCHEMA,
                    a.think,
                    a.temperature,
                    a.num_ctx,
                )
                content = r["message"]["content"]
                attempt.update(
                    wall=r["_wall"],
                    prompt_tokens=r.get("prompt_eval_count"),
                    eval_tokens=r.get("eval_count"),
                    raw=content[:200],
                )
                tag = json.loads(content).get("tag")
                attempt["tag"] = tag
                attempt["verdict"] = "pass" if tag == c["label"] else "fail"
            except json.JSONDecodeError:
                attempt.update(verdict="fail", tag=None, violations=["invalid-json"])
            except Exception as e:  # noqa: BLE001
                attempt.update(verdict="error", error=str(e)[:200])
            row["attempts"].append(attempt)
        verdicts = [
            "fail" if a.get("verdict") == "error" else a.get("verdict", "error")
            for a in row["attempts"]
        ]
        row["verdict"] = majority(verdicts)
        for attempt in row["attempts"]:
            canon = (
                "fail" if attempt.get("verdict") == "error" else attempt.get("verdict")
            )
            if canon == row["verdict"]:
                for k in (
                    "tag",
                    "wall",
                    "prompt_tokens",
                    "eval_tokens",
                    "raw",
                    "violations",
                    "error",
                ):
                    if k in attempt:
                        row[k] = attempt[k]
                break
        row["attempt_details"] = row.pop("attempts")
        results["fence"].append(row)
        print(
            f"  fence   {c['label']:7s} ({c['orig_tag']:10s}) -> {row.get('tag')} {row['verdict']}"
        )

    results["score"] = score(results)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    json.dump(results, open(out / f"{safe}.json", "w"), indent=1)
    print(fmt_score(results))
    if not a.no_rewarm:
        rewarm_fim(host)


def score(res):
    s = res["summary"]
    f = res["fence"]
    n_s = len(s)
    n_f = len(f)
    edits = [r for r in s if r.get("verdict") in ("pass", "fail")]
    walls = [r["wall"] for r in s if "wall" in r]
    tps = [r["decode_tps"] for r in s if r.get("decode_tps")]
    return {
        "summary_pass": sum(r.get("verdict") == "pass" for r in s),
        "summary_report": sum(r.get("verdict") == "report" for r in s),
        "summary_fail": sum(r.get("verdict") == "fail" for r in s),
        "summary_error": sum(r.get("verdict") == "error" for r in s),
        "summary_n": n_s,
        "summary_json_ok": sum(bool(r.get("json_ok")) for r in s),
        "summary_token_loss": sum(bool(r.get("missing_tokens")) for r in edits),
        "summary_schema_fail": sum(
            bool(r.get("violations")) and r.get("violations") != ["fact-token-verify"]
            for r in edits
        ),
        "summary_hallucination": sum(
            "fact-token-verify" in (r.get("violations") or []) for r in edits
        ),
        "fence_pass": sum(r.get("verdict") == "pass" for r in f),
        "fence_n": n_f,
        "wall_p50": statistics.median(walls) if walls else None,
        "decode_tps_p50": statistics.median(tps) if tps else None,
        "load_s": res.get("load_s"),
    }


def fmt_score(res):
    sc = res["score"]
    return (
        f"{res['model']:28s} summary pass {sc['summary_pass']}/{sc['summary_n']} "
        f"(report {sc['summary_report']}, token-loss {sc['summary_token_loss']}, schema {sc['summary_schema_fail']}, "
        f"halluc {sc['summary_hallucination']}, bad-json {sc['summary_n'] - sc['summary_json_ok']}) | "
        f"fence {sc['fence_pass']}/{sc['fence_n']} | wall p50 {sc['wall_p50']:.1f}s decode {sc['decode_tps_p50'] or 0:.0f} tok/s load {sc['load_s']:.1f}s"
    )


def cmd_report(a):
    out = Path(a.out)
    rows = []
    for p in sorted(out.glob("*.json")):
        if p.name == "cases.json" or p.name.startswith("records-"):
            continue
        res = json.load(open(p))
        if "score" in res:
            rows.append(res)
    rows.sort(key=lambda r: (-r["score"]["summary_pass"], -r["score"]["fence_pass"]))
    print(
        "| Model | Ollama | Summary pass | report | token-loss | schema | halluc | bad JSON | Fence | wall p50 | decode | load |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        s = r["score"]
        print(
            f"| `{r['model']}` | {r['ollama']} | **{s['summary_pass']}/{s['summary_n']}** | {s['summary_report']} | {s['summary_token_loss']} | "
            f"{s['summary_schema_fail']} | {s['summary_hallucination']} | {s['summary_n'] - s['summary_json_ok']} | **{s['fence_pass']}/{s['fence_n']}** | "
            f"{s['wall_p50']:.1f} s | {s['decode_tps_p50'] or 0:.0f} tok/s | {s['load_s']:.1f} s |"
        )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--vault", default=str(DEFAULT_VAULT))
    b.add_argument("--out", default=str(DEFAULT_OUT))
    # 21 = 7 per mode across the three corruption modes (was 20 = 5 x 4 before
    # leading-digit was dropped); keep it a multiple of len(modes).
    b.add_argument("--n-summary", type=int, default=21)
    b.add_argument("--n-fence", type=int, default=30)
    b.add_argument("--excerpt-chars", type=int, default=6000)
    b.add_argument(
        "--from-cases",
        default=None,
        help="reuse summary_cases/fence_cases verbatim from an earlier "
        "cases.json; only the skill texts are re-captured from the branch",
    )
    b.set_defaults(fn=cmd_build)
    r = sub.add_parser("run")
    r.add_argument("--model", required=True)
    r.add_argument("--out", default=str(DEFAULT_OUT))
    r.add_argument("--host", default=DEFAULT_HOST)
    r.add_argument(
        "--think", action="store_true", help="leave thinking ON (default: think:false)"
    )
    r.add_argument("--temperature", type=float, default=0.0)
    r.add_argument("--num-ctx", type=int, default=16384)
    r.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="run each case N times; per-case verdict = majority "
        "(ties resolve to the worse outcome)",
    )
    r.add_argument("--no-rewarm", action="store_true")
    r.set_defaults(fn=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    if getattr(a, "repeats", 1) < 1:
        ap.error("--repeats must be >= 1")
    a.fn(a)


if __name__ == "__main__":
    main()
