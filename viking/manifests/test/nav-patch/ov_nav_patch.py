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

Phase 2 (see the section of that name below) replaces this layout for directories
under ``viking://resources/``: code writes the H1, the coverage and one H3 per entry,
and the model writes only the brief, under ``max_tokens``. ``OV_NAV_PHASE2=0`` keeps
Phase 1 everywhere.
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
    text = re.sub(r"[#*`>\[\]]", "", text or "")
    # emphasis underscores only; keep identifiers such as source_file_names intact
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


# Stock framing the summarizer puts in front of the part that distinguishes an entry,
# measured on real v0.4.20 inputs (54 of 55 bugs/resolved summaries open with
# "This document is a bug report and resolution … for BUG-NNNN, which <verb> …").
SUBJECT = (
    r"This (?:[\w-]+\s+){0,2}?"
    r"(?:document|documentation|file|entry|report|directory|specification)"
)
PAYLOAD_AFTER = re.compile(
    rf"^{SUBJECT}\b.*?"
    r"(?:,?\s+(?:which|that)\s+(?:details?|identif(?:y|ies)|address(?:es)?|document(?:s)?|"
    r"describes?|explains?|covers?|records?|tracks?|captures?|outlines?|proposes?|"
    r"summari[sz]es?|specif(?:y|ies)|defines?|investigates?|analy[sz]es?|"
    r"introduces?|establish(?:es)?|ensures?|implements?|adds?|enables?|supports?|"
    r"(?:aims?|aimed|seeks?|sought) to)"
    r"|\s+(?:detailing|describing|documenting|regarding|concerning|addressing|covering|"
    r"outlining|analy[sz]ing|investigating|identifying|explaining|tracking|proposing|"
    r"reporting|summari[sz]ing|specifying|defining|highlighting|examining|evaluating|"
    r"recording|capturing|presenting)"
    r"|\s+(?:log|record|summary|report|analysis|post-mortem)\s+(?:for|of|on)(?=\s+(?:a|an)\s)"
    r"|\s+(?:related to|relating to|focused on|focusing on|about|on the topic of))\s+(.+)$",
    re.IGNORECASE,
)
SELF_ID = re.compile(r"\s*\(\s*[A-Z]+-\d+\s*\)")
PROCESS_NOUN = (
    r"(?:identification|investigation|analysis|root cause analysis|resolution|diagnosis|"
    r"fix|remediation|triage|discovery|implementation|development)"
)
PROCESS_LEAD = re.compile(
    rf"^(?:the\s+)?{PROCESS_NOUN}(?:\s*,\s*(?:and\s+)?{PROCESS_NOUN}|\s+and\s+{PROCESS_NOUN})*"
    r"\s+(?:of|for)\s+",
    re.IGNORECASE,
)
LEAD_IN = re.compile(
    rf"^{SUBJECT}\s+"
    r"(?:is|serves as|contains|provides|describes|documents|captures|records)\s+",
    re.IGNORECASE,
)
SECOND_LEAD_IN = re.compile(
    r"^(?:The (?:primary |main )?(?:purpose|goal|aim) (?:of \S+ )?is to|It (?:details|describes|covers))\s+",
    re.IGNORECASE,
)
# "feature specification and implementation record for …" — measured on real
# features/completed summaries, 2026-09-22.
DOC_TYPE_LEAD = re.compile(
    r"^(?:(?:technical|proposed)\s+)?"
    r"(?:feature|bug|design|implementation|improvement|enhancement)\s+"
    r"(?:request|specification|spec|report|record|design)"
    r"(?:\s+and\s+(?:[\w-]+\s+){0,2}?(?:record|guide|analysis|design|file|registry|log|"
    r"summary|report|research file))?\s+(?:for|of|on)\s+",
    re.IGNORECASE,
)
ENDS_WITH_ID = re.compile(r"\b[A-Z]+-\d+$")
LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
DANGLING = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "due",
    "during",
    "for",
    "from",
    "in",
    "into",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "to",
    "where",
    "which",
    "named",
    "called",
    "while",
    "with",
    "within",
}
MIN_CLAUSE_WORDS = 4
# past participles that, when a clip lands on them, leave a dangling "… caused" /
# "… triggered"; short or noun-like "-ed" words (bed, red, need, seed) excluded
PARTICIPLE_TAIL = re.compile(r"^[a-z]{3,}(?<![eo]e)(?<!ne)ed$")
AUXILIARY = {"is", "are", "was", "were", "be", "been", "being", "get", "got", "gets"}


def _phrase(sentence: str) -> str:
    """One sentence with the summarizer's framing removed (not yet clipped)."""
    s = SELF_ID.sub("", sentence.rstrip(" .;:,!?。？！"))
    m = PAYLOAD_AFTER.match(s)
    text = m.group(1) if m else LEAD_IN.sub("", s)
    text = LEADING_ARTICLE.sub("", text).strip()
    text = DOC_TYPE_LEAD.sub("", text)
    text = PROCESS_LEAD.sub("", LEADING_ARTICLE.sub("", text).strip())
    return LEADING_ARTICLE.sub("", text).strip()


def _weak(text: str) -> bool:
    """A phrase that names only the document type or points back at its own ID."""
    return (
        not text
        or bool(ENDS_WITH_ID.search(text))
        or bool(DOC_TYPE_LEAD.match(text + " for "))
    )


def gloss(summary: str, words: int, name: str | None = None) -> str:
    """The distinguishing phrase of a summary, clipped at a clause boundary.

    Takes the first sentence, drops the summarizer's stock framing ("This document
    is a bug report … for BUG-NNNN, which details …"), clips to ``words`` words at
    the last comma inside the limit when there is one, and strips dangling function
    words. Returns "" when nothing is left or the gloss would only repeat ``name``.
    """
    if words <= 0:
        return ""
    sentences = SENTENCE_END.split(_clean(summary), maxsplit=2)
    text = _phrase(sentences[0])
    if _weak(text) and len(sentences) > 1:
        second = _phrase(SECOND_LEAD_IN.sub("", sentences[1].strip()))
        if second and not _weak(second):
            text = second
    tokens = text.split()
    truncated = len(tokens) > words
    if truncated:
        tokens = _clause_cut(tokens[:words])
    tokens = _trim_tail(tokens, truncated)
    out = " ".join(tokens).rstrip(" .;:,!?。？！—-")
    if name and out.lower().strip() in {name.lower().strip("/"), name.lower()}:
        return ""
    return out


def _clause_cut(cut: list[str]) -> list[str]:
    """Prefer ending at the last comma inside ``cut`` when that keeps a real clause."""
    commas = [i for i, t in enumerate(cut) if t.endswith(",")]
    if commas and commas[-1] + 1 >= MIN_CLAUSE_WORDS:
        return cut[: commas[-1] + 1]
    return cut


def _trim_tail(tokens: list[str], truncated: bool) -> list[str]:
    """Drop dangling function words, and after a clip a ``to <verb>`` or bare participle."""
    tokens = list(tokens)
    while tokens:
        last = tokens[-1].lower().strip(",;:")
        if last in DANGLING:
            tokens.pop()
        elif truncated and len(tokens) > 1 and tokens[-2].lower() == "to":
            # a clipped "to <verb>" ("… optimizing the write path to handle") reads as
            # an unfinished clause; drop both words
            del tokens[-2:]
        elif (
            truncated
            and len(tokens) > MIN_CLAUSE_WORDS
            and PARTICIPLE_TAIL.match(last)
            and tokens[-2].lower() not in AUXILIARY  # "that were dismissed" is complete
        ):
            # a clipped trailing participle ("… outage in the system caused") promises
            # an agent or object that was cut off
            tokens.pop()
        else:
            break
    return tokens


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
        g = gloss(item.get("summary", ""), words, item["name"])
        lines.append(f"- [{item['name']}]({target})" + (f" — {g}." if g else "."))
    for item in children_abstracts:
        target = processor._markdown_link_target(dir_uri, item["name"])
        g = gloss(item.get("abstract", ""), words, item["name"])
        lines.append(f"- [{item['name']}/]({target})" + (f" — {g}." if g else "."))
    missing = (total_files + total_children) - (
        len(file_summaries) + len(children_abstracts)
    )
    if missing > 0:
        lines.append(f"- {missing} further entries are not listed.")
    return "\n".join(lines)


def build_coverage(
    total_files: int, total_children: int, provided: int, where: str = "above"
) -> str:
    total = total_files + total_children
    if provided >= total:
        sentence = (
            f"Total direct entries: {total} ({total_files} files, {total_children} subdirectories); "
            f"all of them are listed {where}."
        )
    else:
        sentence = (
            f"Total direct entries: {total} ({total_files} files, {total_children} subdirectories); "
            f"{provided} are listed {where} and {total - provided} were not individually examined."
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


# --- Phase 2: the model writes only the brief --------------------------------------
#
# Phase 1 kept the model's Detailed Description, whose H3 bodies paraphrase the input
# (~150 of ~1,100 chars) and cover only the entries that fit after the nav. Phase 2
# builds one H3 per entry from the input summaries, so every child gets a link and a
# real summary, and asks the model for the brief alone: the prompt carries glosses, so
# no directory takes the batched merge path (BUG-1172), and the reply is capped.
#
# The H3 bodies are also what ``SemanticDagExecutor._read_existing_summary`` reuses for
# an unchanged file, but only when no freshness debt is pending. A refresh caused by a
# new or changed child has debt pending, and stock v0.4.20 then re-summarizes every
# sampled file (``regenerate_sampled_summary``); the IMPR-1185 canary measured 0 of 58
# sibling inputs from the cache on that path. The bodies are kept idempotent under
# re-clip so the paths that do read them see stable text.
#
#     # <dir>                      code H1
#     <brief>                      model prose, 2-4 sentences (the L0 abstract)
#     ## Directory Coverage        code, counts; "listed below"
#     ## Quick Navigation          code, one H3 per entry with the clipped summary
#
# Scoped by URI prefix (default ``viking://resources/``): memory directories share
# ``_generate_overview`` and keep Phase 1. ``OV_NAV_PHASE2=0`` turns it off at runtime.

PHASE2_DEFAULT_PREFIXES = "viking://resources/"
BRIEF_MAX_TOKENS = 768
BRIEF_MAX_CHARS = 1200
BRIEF_GLOSS_WORDS = 20
MAX_BODY_CHARS = 500
MIN_BODY_CHARS = 40
CONTENTS_HEADING = "## Quick Navigation"
TERMINAL = " .;:,!?。？！—-"
BRIEF_LABEL = re.compile(
    r"^(?:brief description|description|overview|summary)\s*[:：]\s*", re.IGNORECASE
)
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# Sentences that describe the document's layout rather than its subject ("The file
# covers the problem description, root cause analysis, …"), measured on real
# summaries; they spend a body's budget and tell entries apart by nothing.
STRUCTURE_SENTENCE = re.compile(
    r"^(?:The|This|It)\b(?:\s+[\w-]+){0,3}?\s+(?:is structured|is organized|is divided|"
    r"consists of|covers the (?:problem|context|metadata|background)|"
    r"includes (?:a |the )?(?:metadata|sections?|a header))",
    re.IGNORECASE,
)

BRIEF_PROMPT = """Output Language: {output_language}
Write only in {output_language}.

You are writing the opening paragraph of an overview for the directory "{dir_name}". A complete list of its entries, each with a link and a summary, is added automatically after your text. Do not list the entries, and write no headings, links, bullet points or tables.

Write 2 to 4 sentences of plain prose about what the directory holds as a whole: its main subjects, the themes that recur, and how the entries relate. The first sentence says what the directory holds and is used on its own as the directory's one-line abstract, so keep it under 200 characters.

Describe only what the entries below state; do not invent facts.{coverage_hint}

[Entries]
{entries}
"""


def phase2_enabled(dir_uri: str) -> bool:
    if os.environ.get("OV_NAV_PHASE2", "1") == "0":
        return False
    prefixes = tuple(
        p.strip()
        for p in os.environ.get(
            "OV_NAV_PHASE2_PREFIXES", PHASE2_DEFAULT_PREFIXES
        ).split(",")
        if p.strip()
    )
    return bool(prefixes) and dir_uri.startswith(prefixes)


def _as_sentence(text: str) -> str:
    text = text.strip().rstrip(TERMINAL)
    return f"{text[0].upper()}{text[1:]}." if text else ""


def _is_name(text: str, name: str | None) -> bool:
    return bool(name) and text.lower().strip() in {
        name.lower().strip("/"),
        name.lower(),
    }


def clip_body(text: str, budget: int) -> str:
    """Clip to ``budget`` chars, preferring a sentence end, else a clause boundary.

    A text already within ``budget`` comes back unchanged, so re-clipping a cached
    body at the same budget is a no-op.
    """
    if len(text) <= budget:
        return text
    if budget < MIN_BODY_CHARS:
        return ""
    ends = [m.end() for m in re.finditer(r"[.!?。？！](?=\s|$)", text[:budget])]
    if ends and ends[-1] >= budget // 2:
        return text[: ends[-1]].strip()
    tokens: list[str] = []
    for token in text.split():
        if len(" ".join(tokens + [token])) + 1 > budget:
            break
        tokens.append(token)
    tokens = _trim_tail(_clause_cut(tokens), truncated=True)
    return _as_sentence(" ".join(tokens))


def entry_body(summary: str, budget: int, name: str | None = None) -> str:
    """The cached summary body for one entry: framing stripped, clipped to ``budget``.

    Idempotent: ``entry_body(entry_body(s, b), b) == entry_body(s, b)``, so a body
    read back from the cache and rebuilt on the next regeneration does not drift.
    """
    if budget <= 0:
        return ""
    sentences = [s for s in SENTENCE_END.split(_clean(summary)) if s.strip()]
    first = ""
    while sentences and (not first or _is_name(first, name)):
        first = _phrase(SECOND_LEAD_IN.sub("", sentences.pop(0).strip()))
    if _is_name(first, name):
        first = ""
    if _weak(first) and sentences:
        second = _phrase(SECOND_LEAD_IN.sub("", sentences[0].strip()))
        if second and not _weak(second):
            first = second
            sentences.pop(0)
    parts = [_as_sentence(first)] + [
        _as_sentence(s) for s in sentences if not STRUCTURE_SENTENCE.match(s.strip())
    ]
    return clip_body(" ".join(p for p in parts if p), min(budget, MAX_BODY_CHARS))


def _contents_entries(processor, dir_uri, file_summaries, children_abstracts):
    entries = []
    for item in file_summaries:
        target = processor._markdown_link_target(dir_uri, item["name"])
        entries.append(
            (f"### [{item['name']}]({target})", item.get("summary", ""), item["name"])
        )
    for item in children_abstracts:
        target = processor._markdown_link_target(dir_uri, item["name"])
        entries.append(
            (f"### [{item['name']}/]({target})", item.get("abstract", ""), item["name"])
        )
    return entries


def build_contents(
    processor, dir_uri: str, file_summaries, children_abstracts, space: int
) -> str:
    """One H3 per entry, bodies sized so the whole block fits in ``space`` chars."""
    entries = _contents_entries(processor, dir_uri, file_summaries, children_abstracts)
    if not entries:
        return ""
    fixed = len(CONTENTS_HEADING) + sum(len(h) + 4 for h, _, _ in entries)
    budget = (space - fixed) // len(entries)
    while True:
        blocks = [CONTENTS_HEADING]
        for heading, summary, name in entries:
            body = entry_body(summary, budget, name)
            blocks.append(f"{heading}\n\n{body}" if body else heading)
        text = "\n\n".join(blocks)
        if len(text) <= space or budget < MIN_BODY_CHARS:
            return text
        budget -= max(1, (len(text) - space) // len(entries) + 1)


def build_brief_prompt(
    dir_uri: str,
    file_summaries,
    children_abstracts,
    total_files: int,
    total_children: int,
    output_language: str,
) -> str:
    lines = []
    for item in file_summaries:
        g = gloss(item.get("summary", ""), BRIEF_GLOSS_WORDS, item["name"])
        lines.append(f"- {item['name']}" + (f": {g}" if g else ""))
    for item in children_abstracts:
        g = gloss(item.get("abstract", ""), BRIEF_GLOSS_WORDS, item["name"])
        lines.append(f"- {item['name']}/ (subdirectory)" + (f": {g}" if g else ""))
    total = total_files + total_children
    provided = len(file_summaries) + len(children_abstracts)
    hint = (
        f" Only {provided} of the {total} entries are shown; hedge any generalization"
        ' ("the sample shows …").'
        if provided < total
        else ""
    )
    return BRIEF_PROMPT.format(
        output_language=output_language,
        dir_name=dir_uri.rstrip("/").split("/")[-1],
        coverage_hint=hint,
        entries="\n".join(lines) or "None",
    )


def sanitize_brief(processor, raw: str) -> str:
    """Plain prose from the model's reply: no headings, lists, tables or links.

    The brief is the prose before the first heading that follows some prose, so a
    leading H1 or "## Brief Description" is skipped and anything the model wrote in
    later sections is dropped.
    """
    kept: list[str] = []
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if kept:
                break
            continue
        if (
            stripped
            and not stripped.startswith(("|", ">", "```"))
            and not re.match(r"^(?:[-*+]|\d+[.)])\s", stripped)
        ):
            kept.append(stripped)
    text = " ".join(kept)
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = re.sub(r"\S*viking://\S*", "", text).replace("**", "")
    text = BRIEF_LABEL.sub("", re.sub(r"\s+", " ", text).strip())
    if any(marker in text for marker in FAILURE_MARKERS):
        return ""
    if text and text[-1] not in ".!?。？！":
        ends = [m.end() for m in re.finditer(r"[.!?。？！](?=\s|$)", text)]
        text = text[: ends[-1]] if ends else ""  # cut off by max_tokens mid-sentence
    return processor._truncate_generated_text(text.strip(), BRIEF_MAX_CHARS)


def assemble_contents(
    processor,
    raw_brief: str,
    dir_uri: str,
    file_summaries,
    children_abstracts,
    total_files: int | None,
    total_children: int | None,
    cap: int,
) -> str:
    """Pure transform: model brief + inputs into the Phase 2 layout, within ``cap``."""
    total_files = len(file_summaries) if total_files is None else total_files
    total_children = (
        len(children_abstracts) if total_children is None else total_children
    )
    provided = len(file_summaries) + len(children_abstracts)
    brief = sanitize_brief(processor, raw_brief)
    name = dir_uri.rstrip("/").split("/")[-1]
    head = (
        f"# {name}\n\n{brief}"
        if brief
        else fallback_head(dir_uri, total_files, total_children)
    )
    coverage = build_coverage(total_files, total_children, provided, where="below")
    fixed = f"{head}\n\n{coverage}"
    contents = build_contents(
        processor,
        dir_uri,
        file_summaries,
        children_abstracts,
        cap - len(fixed) - 2,
    )
    return f"{fixed}\n\n{contents}" if contents else fixed


def _accepts_max_tokens(func) -> bool:
    try:
        return "max_tokens" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def _output_language(config, text: str) -> str:
    try:
        from openviking.session.memory.utils.language import resolve_output_language

        return resolve_output_language(text, config=config)
    except Exception:  # noqa: BLE001 — language detection is best-effort
        return "en"


async def generate_brief_overview(
    module, processor, dir_uri, file_summaries, children_abstracts, tf, tc
):
    """Phase 2 replacement body. Returns None when the VLM is unavailable."""
    import contextlib

    config = module.get_openviking_config()
    vlm = config.vlm
    if not vlm.is_available():
        return None
    tf = len(file_summaries) if tf is None else tf
    tc = len(children_abstracts) if tc is None else tc
    language = _output_language(
        config,
        "\n".join(
            [x.get("summary", "") for x in file_summaries]
            + [x.get("abstract", "") for x in children_abstracts]
        )
        or dir_uri,
    )
    prompt = build_brief_prompt(
        dir_uri, file_summaries, children_abstracts, tf, tc, language
    )
    kwargs = (
        {"max_tokens": BRIEF_MAX_TOKENS}
        if _accepts_max_tokens(vlm.get_completion_async)
        else {}
    )
    stage = getattr(module, "bind_telemetry_stage", None)
    raw = ""
    try:
        with stage("resource_summarize") if stage else contextlib.nullcontext():
            raw = await vlm.get_completion_async(prompt, **kwargs)
    except Exception:
        logger.exception(
            "ov-nav-patch: brief generation failed for %s; code brief used", dir_uri
        )
    cap = config.semantic.overview_max_chars
    return assemble_contents(
        processor,
        raw if isinstance(raw, str) else "",
        dir_uri,
        file_summaries,
        children_abstracts,
        tf,
        tc,
        cap,
    )


def phase2_self_check(cls) -> bool:
    """Round-trip a Phase 2 overview through the installed parsers.

    Phase 2 relies on ``_parse_overview_md`` keying linked H3s to the entry name and
    on ``_extract_abstract_from_overview`` returning the brief; if either no longer
    holds, Phase 2 stays off and Phase 1 is used.
    """
    try:
        p = object.__new__(cls)
        fs = [
            {"name": "a.md", "summary": "Alpha entry about caching. It has detail."},
            {"name": "b c.md", "summary": "Beta entry."},
        ]
        cs = [{"name": "sub", "abstract": "Subdirectory with archived entries."}]
        doc = assemble_contents(
            p, "Brief prose.", "viking://resources/x/d", fs, cs, None, None, 20000
        )
        cache = p._parse_overview_md(doc)
        return (
            cache.get("a.md") == "Alpha entry about caching. It has detail."
            and cache.get("b c.md") == "Beta entry."
            and p._extract_abstract_from_overview(doc) == "Brief prose."
        )
    except Exception:
        logger.exception("ov-nav-patch: phase 2 self-check raised")
        return False


def _source_hash(func) -> str | None:
    try:
        return hashlib.sha256(inspect.getsource(func).encode()).hexdigest()
    except (OSError, TypeError):
        return None


def dump_inputs(
    dir_uri, file_summaries, children_abstracts, total_files, total_children, raw, out
) -> None:
    """Opt-in capture of one generation's real inputs, for tuning the glosses offline.

    Writes nothing unless OV_NAV_PATCH_DUMP names a directory, and never raises:
    a dump failure must not affect the overview that is returned.
    """
    target = os.environ.get("OV_NAV_PATCH_DUMP")
    if not target:
        return
    try:
        import json
        import time

        os.makedirs(target, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", dir_uri)[-120:]
        path = os.path.join(target, f"{slug}-{time.time_ns()}-{os.getpid()}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "dir_uri": dir_uri,
                    "file_summaries": list(file_summaries),
                    "children_abstracts": list(children_abstracts),
                    "total_files": total_files,
                    "total_children": total_children,
                    "raw": raw,
                    "out": out,
                },
                fh,
                ensure_ascii=False,
            )
    except Exception:
        logger.exception("ov-nav-patch: input dump failed for %s", dir_uri)


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

    phase2_ok = phase2_self_check(cls)
    if not phase2_ok:
        logger.warning("ov-nav-patch: phase 2 self-check failed; phase 1 only")

    async def _generate_overview(
        self,
        dir_uri,
        file_summaries,
        children_abstracts,
        llm_sem=None,
        total_files=None,
        total_children=None,
    ):
        if phase2_ok and phase2_enabled(dir_uri):
            try:
                out = await generate_brief_overview(
                    module,
                    self,
                    dir_uri,
                    file_summaries,
                    children_abstracts,
                    total_files,
                    total_children,
                )
            except Exception:
                logger.exception(
                    "ov-nav-patch: phase 2 failed for %s; falling back to phase 1",
                    dir_uri,
                )
                out = None
            if out is not None:
                dump_inputs(
                    dir_uri,
                    file_summaries,
                    children_abstracts,
                    total_files,
                    total_children,
                    "",
                    out,
                )
                return out
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
            out = assemble(
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
        dump_inputs(
            dir_uri,
            file_summaries,
            children_abstracts,
            total_files,
            total_children,
            raw,
            out,
        )
        return out

    _generate_overview._ov_nav_patch = True
    _generate_overview.__wrapped__ = orig
    cls._generate_overview = _generate_overview
    logger.warning(
        "ov-nav-patch: applied to SemanticProcessor._generate_overview (pid %s, phase 2 %s)",
        os.getpid(),
        "on" if phase2_ok else "off",
    )
    return True
