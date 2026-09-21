"""Deterministic Quick Navigation for OpenViking v0.4.20 directory overviews (IMPR-1185).

The stock ``SemanticProcessor._generate_overview`` asks the VLM to write the whole
overview, including one navigation line per entry with an exact link placeholder.
Every navigation defect in compendium BUG-1155 / BUG-1169 / BUG-1170 / BUG-1172
comes from that structured emission. Code already holds the child inventory, so
this wrapper keeps the model's prose and replaces its navigation and coverage
sections with lists built from the inputs.

Output layout (the order is load-bearing):

    # <dir>                      model H1
    <Brief Description>          model prose; the L0 abstract is extracted from it
    ## Quick Navigation          code-built, one line per entry, real URIs
    ## Directory Coverage        code-built from the counts
    ## Detailed Description      model prose, pre-truncated to the space left

Constraints honoured (v0.4.20 source):
  * ``_extract_abstract_from_overview`` reads prose after the H1 up to the first
    ``##``, so navigation sits under an ``##`` AFTER the brief.
  * ``_parse_overview_md`` treats every ``###`` body as a cached per-file summary and
    keeps appending until the next ``###``, so nothing code-generated may follow
    the model's last H3; everything added here precedes the first H3.
  * ``_enforce_size_limits`` tail-cuts at a sentence boundary; the tail is sized so
    the document is already under ``overview_max_chars`` and every nav line ends in
    a period as a backstop.

Guarded: applies only to openviking ``v0.4.20`` whose ``_generate_overview`` source
hashes to ``EXPECTED_SOURCE_SHA256``; otherwise it logs and leaves stock behaviour.
Model failures (the stock placeholder strings) are passed through unchanged.
Disable at runtime with ``OV_NAV_PATCH=0``.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import re

EXPECTED_VERSION = "v0.4.20"
EXPECTED_SOURCE_SHA256 = (
    "aec8040007bbfed22727309060fc7034a325a738925e65024337d1aefe71325f"
)
NAV_SHARE = 0.6  # nav may take at most this share of overview_max_chars
MAX_GLOSS_WORDS = 12
FAILURE_MARKERS = (
    "[Directory overview is not generated]",
    "[Directory overview is not ready]",
)
NAV_HEADINGS = {"quick navigation", "快速导航"}
COVERAGE_HEADINGS = {"directory coverage", "目录覆盖"}
MANGLED_PLACEHOLDER = re.compile(
    r"(?<![\w/])(?:viking|v)://(?:input)*input_sample_([fc]\d+)"
)
SENTENCE_END = re.compile(r"(?<=[.!?。？！])\s")

logger = logging.getLogger("ov_nav_patch")


def build_link_map(
    processor, dir_uri: str, file_summaries, children_abstracts
) -> dict[str, str]:
    """Rebuild the stock placeholder map (same numbering as _generate_overview)."""
    link_map: dict[str, str] = {}
    for idx, item in enumerate(file_summaries, 1):
        link_map[f"f{idx}"] = processor._markdown_link_target(dir_uri, item["name"])
    for idx, item in enumerate(children_abstracts, 1):
        link_map[f"c{idx}"] = processor._markdown_link_target(dir_uri, item["name"])
    return link_map


def repair_placeholders(text: str, link_map: dict[str, str]) -> str:
    """Resolve placeholders the model mangled (``v://…``, ``inputinput_…``) — BUG-1170."""

    def sub(match: re.Match) -> str:
        return link_map.get(match.group(1), match.group(0))

    return MANGLED_PLACEHOLDER.sub(sub, text)


def split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split into (head, [(h2_title, section_text)]) on lines starting with ``## ``."""
    head_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            head_lines.append(line)
    return "\n".join(head_lines).strip(), [
        (t, "\n".join(ls).strip()) for t, ls in sections
    ]


def _clean(text: str) -> str:
    text = re.sub(r"[#*_`>\[\]]", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def gloss(summary: str, words: int) -> str:
    """First sentence of a summary, clipped to ``words`` words, no trailing punctuation."""
    if words <= 0:
        return ""
    first = SENTENCE_END.split(_clean(summary), maxsplit=1)[0]
    clipped = " ".join(first.split()[:words])
    return clipped.rstrip(" .;:,!?。？！—-")


def build_nav(
    processor,
    dir_uri: str,
    file_summaries,
    children_abstracts,
    total_files: int,
    total_children: int,
    words: int,
) -> str:
    lines = ["## Quick Navigation", ""]
    for item in file_summaries:
        target = processor._markdown_link_target(dir_uri, item["name"])
        g = gloss(item.get("summary", ""), words)
        lines.append(f"- [{item['name']}]({target})" + (f" — {g}." if g else "."))
    for item in children_abstracts:
        target = processor._markdown_link_target(dir_uri, item["name"])
        g = gloss(item.get("abstract", ""), words)
        lines.append(f"- [{item['name']}/]({target})" + (f" — {g}." if g else "."))
    missing = (total_files + total_children) - (
        len(file_summaries) + len(children_abstracts)
    )
    if missing > 0:
        lines.append(f"- {missing} further entries are not listed.")
    return "\n".join(lines)


def build_coverage(total_files: int, total_children: int, provided: int) -> str:
    total = total_files + total_children
    if provided >= total:
        sentence = (
            f"Total direct entries: {total} ({total_files} files, {total_children} subdirectories); "
            "all of them are listed above."
        )
    else:
        sentence = (
            f"Total direct entries: {total} ({total_files} files, {total_children} subdirectories); "
            f"{provided} are listed above and {total - provided} were not individually examined."
        )
    return f"## Directory Coverage\n\n{sentence}"


def fallback_head(dir_uri: str, total_files: int, total_children: int) -> str:
    name = dir_uri.rstrip("/").split("/")[-1]
    return (
        f"# {name}\n\n"
        f"Directory {name} with {total_files} files and {total_children} subdirectories."
    )


def assemble(
    processor,
    raw: str,
    dir_uri: str,
    file_summaries,
    children_abstracts,
    total_files: int | None,
    total_children: int | None,
    cap: int,
) -> str:
    """Pure transform of the model's raw overview into the deterministic layout."""
    if any(marker in raw for marker in FAILURE_MARKERS):
        return raw
    total_files = len(file_summaries) if total_files is None else total_files
    total_children = (
        len(children_abstracts) if total_children is None else total_children
    )
    provided = len(file_summaries) + len(children_abstracts)

    raw = repair_placeholders(
        raw, build_link_map(processor, dir_uri, file_summaries, children_abstracts)
    )
    head, sections = split_sections(raw)
    head_prose = [
        ln for ln in head.split("\n") if ln.strip() and not ln.startswith("#")
    ]
    if not head_prose:
        head = fallback_head(dir_uri, total_files, total_children)

    kept = [
        text
        for title, text in sections
        if title.strip().lower() not in NAV_HEADINGS | COVERAGE_HEADINGS
    ]

    nav = ""
    for words in range(MAX_GLOSS_WORDS, -1, -1):
        nav = build_nav(
            processor,
            dir_uri,
            file_summaries,
            children_abstracts,
            total_files,
            total_children,
            words,
        )
        if len(nav) <= int(cap * NAV_SHARE):
            break
    coverage = build_coverage(total_files, total_children, provided)

    fixed = f"{head}\n\n{nav}\n\n{coverage}"
    budget = cap - len(fixed) - 2
    tail = "\n\n".join(kept).strip()
    if tail and budget > 0:
        tail = processor._truncate_generated_text(tail, budget)
        return f"{fixed}\n\n{tail}"
    return fixed


def _source_hash(func) -> str | None:
    try:
        return hashlib.sha256(inspect.getsource(func).encode()).hexdigest()
    except (OSError, TypeError):
        return None


def apply(module) -> bool:
    """Patch ``module.SemanticProcessor._generate_overview`` in place; True if applied."""
    if os.environ.get("OV_NAV_PATCH", "1") == "0":
        logger.warning("ov-nav-patch: disabled by OV_NAV_PATCH=0")
        return False
    import openviking

    version = getattr(openviking, "__version__", None)
    cls = getattr(module, "SemanticProcessor", None)
    orig = getattr(cls, "_generate_overview", None)
    if getattr(orig, "_ov_nav_patch", False):
        return True
    digest = _source_hash(orig) if orig else None
    if version != EXPECTED_VERSION or digest != EXPECTED_SOURCE_SHA256:
        logger.warning(
            "ov-nav-patch: NOT applied (version=%r source_sha256=%s); stock overview behaviour kept",
            version,
            digest,
        )
        return False

    async def _generate_overview(
        self,
        dir_uri,
        file_summaries,
        children_abstracts,
        llm_sem=None,
        total_files=None,
        total_children=None,
    ):
        raw = await orig(
            self,
            dir_uri,
            file_summaries,
            children_abstracts,
            llm_sem,
            total_files,
            total_children,
        )
        try:
            cap = module.get_openviking_config().semantic.overview_max_chars
            return assemble(
                self,
                raw,
                dir_uri,
                file_summaries,
                children_abstracts,
                total_files,
                total_children,
                cap,
            )
        except Exception:
            logger.exception(
                "ov-nav-patch: assemble failed for %s; returning stock output", dir_uri
            )
            return raw

    _generate_overview._ov_nav_patch = True
    _generate_overview.__wrapped__ = orig
    cls._generate_overview = _generate_overview
    logger.warning(
        "ov-nav-patch: applied to SemanticProcessor._generate_overview (pid %s)",
        os.getpid(),
    )
    return True
