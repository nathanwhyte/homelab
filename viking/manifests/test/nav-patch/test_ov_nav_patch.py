"""Tests for ov_nav_patch against the INSTALLED openviking v0.4.20 methods.

Run inside the openviking image (no pytest needed):
    PYTHONPATH=<dir> /app/.venv/bin/python <dir>/test_ov_nav_patch.py
"""

import sys
import types

import ov_nav_patch as nav
from openviking.storage.queuefs import semantic_processor as sp

P = object.__new__(sp.SemanticProcessor)  # the methods used here need no instance state
DIR = "viking://resources/compendium/bugs/resolved"
CAP = 20000


def files(n, prefix="bug"):
    return [
        {
            "name": f"{prefix}-{i}.md",
            "summary": f"Summary of {prefix} {i}. It fixes a thing in module {i}. More.",
        }
        for i in range(1, n + 1)
    ]


def children(n):
    return [
        {
            "name": f"sub{i}",
            "abstract": f"Subdirectory {i} holds archived items. Second sentence.",
        }
        for i in range(1, n + 1)
    ]


def targets(fs, cs):
    return {P._markdown_link_target(DIR, x["name"]) for x in fs + cs}


def links_in(text):
    import re

    return set(re.findall(r"\]\((viking://[^)\s]+)\)", text))


def model_output(fs, cs, *, mangle_h3=False, drop_nav_tail=0):
    """A plausible model reply after the stock _replace_link_references ran."""
    lines = [
        "# resolved",
        "",
        "Resolved bugs across dipdash, mage and homelab, with fixes and root causes.",
        "",
        "## Quick Navigation",
    ]
    for i, x in enumerate(fs[: len(fs) - drop_nav_tail], 1):
        lines.append(
            f"- [{x['name']}]({P._markdown_link_target(DIR, x['name'])}) — model gloss."
        )
    lines += [
        "",
        "## Directory Coverage",
        "",
        "All entries are represented.",
        "",
        "## Detailed Description",
        "",
    ]
    for i, x in enumerate(fs, 1):
        target = (
            f"v://input_sample_f{i}"
            if (mangle_h3 and i == 2)
            else P._markdown_link_target(DIR, x["name"])
        )
        lines += [
            f"### [{x['name']}]({target})",
            "",
            f"One sentence about {x['name']}.",
            "",
        ]
    return "\n".join(lines)


def test_single_shot_nav_complete_and_ordered():
    fs, cs = files(8), children(3)
    out = nav.assemble(
        P, model_output(fs, cs, drop_nav_tail=3), DIR, fs, cs, None, None, CAP
    )
    head_end = out.index("## Quick Navigation")
    assert out.index("## Directory Coverage") > head_end
    assert out.index("## Detailed Description") > out.index("## Directory Coverage")
    nav_block = out[head_end : out.index("## Directory Coverage")]
    assert targets(fs, cs) <= links_in(nav_block), (
        "every file and subdirectory must be linked in nav"
    )
    assert (
        out.count("## Quick Navigation") == 1
        and out.count("## Directory Coverage") == 1
    )
    assert "input_sample" not in out


def test_abstract_is_brief_not_nav():
    fs, cs = files(5), children(2)
    out = nav.assemble(P, model_output(fs, cs), DIR, fs, cs, None, None, CAP)
    abstract = P._extract_abstract_from_overview(out)
    assert abstract.startswith("Resolved bugs"), abstract
    assert "viking://" not in abstract


def test_summary_cache_unpolluted():
    fs, cs = files(6), children(2)
    out = nav.assemble(P, model_output(fs, cs), DIR, fs, cs, None, None, CAP)
    cache = P._parse_overview_md(out)
    assert set(cache) == {x["name"] for x in fs}, cache.keys()
    for body in cache.values():
        assert "Quick Navigation" not in body and "Directory Coverage" not in body
        assert "further entries" not in body


def test_mangled_placeholder_repaired():
    fs, cs = files(4), []
    out = nav.assemble(
        P, model_output(fs, cs, mangle_h3=True), DIR, fs, cs, None, None, CAP
    )
    assert "v://input_sample" not in out
    assert (
        f"### [{fs[1]['name']}]({P._markdown_link_target(DIR, fs[1]['name'])})" in out
    )


def test_inputinput_variant_repaired():
    fs = files(3)
    raw = "# d\n\nBrief text here.\n\n## Detailed Description\n\n### [x](viking://inputinput_sample_f3)\n\nBody."
    out = nav.assemble(P, raw, DIR, fs, [], None, None, CAP)
    assert "input_sample" not in out


def test_failure_passthrough():
    for marker in nav.FAILURE_MARKERS:
        raw = f"# resolved\n\n{marker}"
        assert nav.assemble(P, raw, DIR, files(3), [], None, None, CAP) == raw


def test_missing_brief_gets_code_brief():
    fs, cs = files(3), children(1)
    raw = "# resolved\n\n## Quick Navigation\n\n- [a](b) — c."
    out = nav.assemble(P, raw, DIR, fs, cs, None, None, CAP)
    abstract = P._extract_abstract_from_overview(out)
    assert abstract.startswith(
        "Directory resolved with 3 files and 1 subdirectories"
    ), abstract


def test_large_directory_fits_cap_with_complete_nav():
    fs, cs = files(99, "task"), children(6)
    out = nav.assemble(P, model_output(fs, cs), DIR, fs, cs, None, None, CAP)
    assert len(out) <= CAP, len(out)
    assert P._truncate_generated_text(out, CAP) == out, (
        "stock size limit must find nothing to cut"
    )
    nav_block = out[
        out.index("## Quick Navigation") : out.index("## Directory Coverage")
    ]
    assert targets(fs, cs) <= links_in(nav_block)
    assert all(ln.endswith(".") for ln in nav_block.splitlines() if ln.startswith("- "))


def test_small_cap_shrinks_gloss_not_entries():
    fs, cs = files(60), children(4)
    out = nav.assemble(P, model_output(fs, cs), DIR, fs, cs, None, None, 6000)
    nav_block = out[
        out.index("## Quick Navigation") : out.index("## Directory Coverage")
    ]
    assert targets(fs, cs) <= links_in(nav_block)


def test_sampled_directory_states_the_gap():
    fs, cs = files(10), children(2)
    out = nav.assemble(P, model_output(fs, cs), DIR, fs, cs, 150, 5, CAP)
    assert "- 143 further entries are not listed." in out
    assert "12 are listed above and 143 were not individually examined" in out


# First sentences of REAL v0.4.20 file summaries and child abstracts, captured on
# ov-test with OV_NAV_PATCH_DUMP (bugs/resolved, 2026-09-22). Each pins one framing
# the summarizer uses in front of the part that tells entries apart.
REAL_GLOSSES = [
    (
        "bug-1002.md",
        (
            "This document is a bug report and resolution log for BUG-1002, which details a "
            "network timeout issue during large Compendium to OpenViking data backfills."
        ),
        "network timeout issue during large Compendium to OpenViking data backfills",
    ),
    (
        "bug-1006.md",
        (
            "This document is a bug report (BUG-1006) detailing a technical issue where the "
            "OpenViking FS API rejected client requests due to missing tenant headers."
        ),
        "technical issue where the OpenViking FS API rejected client requests",
    ),
    (
        "bug-1009.md",
        (
            "This document is a bug report and resolution log for a rendering corruption issue "
            "involving Claude Code when used within a tmux session under the Ghostty terminal."
        ),
        "rendering corruption issue involving Claude Code when used within a tmux session",
    ),
    (
        "bug-1070.md",
        (
            "This document is a bug report (BUG-1070) detailing the identification, root cause "
            "analysis, and resolution of an orphaned git conflict marker committed within a "
            "specific guide."
        ),
        "orphaned git conflict marker committed within a specific guide",
    ),
    (
        "bug-000.md",
        (
            "This document serves as a meta-entry triage register for internal DipDash bug "
            "reports that were dismissed, identified as configuration issues, or confirmed as "
            "already fixed."
        ),
        "meta-entry triage register for internal DipDash bug reports that were dismissed",
    ),
    (
        "robots",
        (
            "This directory contains technical documentation and bug reports related to AI "
            "image generation."
        ),
        "AI image generation",
    ),
    # features/completed framings (captured 2026-09-22): a doc-type lead-in, "which
    # aims to …" verbs, and identifiers that must keep their underscores.
    (
        "feat-020.md",
        (
            "This document is a technical feature specification for FEAT-020, which aims "
            "to establish source_file_names as the single source of truth for file_type "
            "within the dipdash system."
        ),
        "establish source_file_names as the single source of truth for file_type",
    ),
    (
        "feat-021.md",
        (
            "This document is a feature specification and research file for FEAT-021, "
            "which aimed to support creating and onboarding new company pipelines via the "
            "Platform API."
        ),
        "support creating and onboarding new company pipelines via the Platform API",
    ),
    (
        "feat-1014.md",
        (
            "This document is a feature specification and implementation record for "
            "FEAT-1014, which introduces a tool to sync yt-dlp media from a K3s cluster to "
            "a local archive."
        ),
        "tool to sync yt-dlp media from a K3s cluster to a local",
    ),
]


def test_gloss_falls_back_to_the_second_sentence_when_the_first_is_only_a_doc_type():
    summary = (
        "This document is a technical feature specification for FEAT-777. "
        "The primary purpose is to remove duplicated file_type fields from the loader."
    )
    got = nav.gloss(summary, nav.MAX_GLOSS_WORDS, "feat-777.md")
    assert got == "remove duplicated file_type fields from the loader", got


# Open-item framings seen in PROD after the 2026-09-22 rollout (bugs/homelab,
# improvements/homelab). Reconstructed from the stored glosses, not captured raw:
# "bug report identifying …" and "proposed improvement specification for …".
OPEN_ITEM_GLOSSES = [
    (
        "bug-1153.md",
        (
            "This document is a bug report identifying a service outage in the OpenViking "
            "system caused by exhausted Ollama Cloud session quota and a missing backup."
        ),
        "service outage in the OpenViking system caused by exhausted Ollama Cloud session",
    ),
    (
        "impr-1175.md",
        (
            "This document is a proposed improvement specification for optimizing the "
            "OpenViking write path to handle claude-mem scale volume with a fast cloud VLM."
        ),
        "optimizing the OpenViking write path to handle claude-mem scale volume",
    ),
    (
        "bug-1121.md",
        (
            "This document is a bug report identifying an issue where a successful bulk "
            "compendium-sync fails to update the sync state file."
        ),
        "issue where a successful bulk compendium-sync fails to update the sync state",
    ),
]


def test_gloss_handles_open_item_framings():
    for name, summary, expected in OPEN_ITEM_GLOSSES:
        got = nav.gloss(summary, nav.MAX_GLOSS_WORDS, name)
        assert got == expected, (name, got)


def test_gloss_clip_never_ends_on_to_verb_or_bare_participle():
    to_verb = (
        "This document is a bug report identifying an effort to rebalance the storage "
        "tier so the vector index can handle much larger nightly import batches."
    )
    got = nav.gloss(to_verb, 9, "x.md")
    assert got.split()[-2:-1] != ["to"], got
    participle = (
        "This document is a bug report identifying a service outage in the platform "
        "caused by a stale credential cache on the gateway."
    )
    for words in (7, 8):
        got = nav.gloss(participle, words, "x.md")
        assert not got.endswith("caused"), (words, got)
    # a real noun ending in -ed is kept
    assert nav.gloss("This document covers the shared seed.", 12, "x.md").endswith(
        "seed"
    )


def test_gloss_extracts_the_distinguishing_phrase_from_real_summaries():
    for name, summary, expected in REAL_GLOSSES:
        got = nav.gloss(summary, nav.MAX_GLOSS_WORDS, name)
        assert got == expected, (name, got)


def test_gloss_never_starts_with_boilerplate_or_ends_dangling():
    for name, summary, _ in REAL_GLOSSES:
        for words in (12, 8, 5, 3, 1):
            got = nav.gloss(summary, words, name)
            assert not got.lower().startswith(("this ", "bug report", "document")), got
            assert not got or got.split()[-1].lower() not in nav.DANGLING, (words, got)
            assert len(got.split()) <= words, (words, got)


def test_gloss_drops_a_name_only_abstract():
    assert nav.gloss("dipdash", 12, "dipdash") == ""
    assert nav.gloss("dipdash.", 12, "dipdash/") == ""
    fs, cs = [], [{"name": "dipdash", "abstract": "dipdash"}]
    line = nav.build_nav(P, DIR, fs, cs, 0, 1, 12).splitlines()[-1]
    assert line == f"- [dipdash/]({P._markdown_link_target(DIR, 'dipdash')}).", line


def test_dump_inputs_is_opt_in_and_never_raises(tmp=None):
    import json
    import os
    import tempfile

    os.environ.pop("OV_NAV_PATCH_DUMP", None)
    nav.dump_inputs(
        DIR, files(2), [], 2, 0, "raw", "out"
    )  # disabled: no error, no file
    with tempfile.TemporaryDirectory() as d:
        os.environ["OV_NAV_PATCH_DUMP"] = d
        try:
            nav.dump_inputs(DIR, files(2), children(1), 2, 1, "raw", "out")
            dumped = []
            for f in os.listdir(d):
                with open(os.path.join(d, f)) as fh:
                    dumped.append(json.load(fh))
            assert len(dumped) == 1 and dumped[0]["dir_uri"] == DIR
            assert dumped[0]["total_children"] == 1 and dumped[0]["out"] == "out"
            os.environ["OV_NAV_PATCH_DUMP"] = "/proc/forbidden/navdump"
            nav.dump_inputs(DIR, files(1), [], 1, 0, "raw", "out")  # must not raise
        finally:
            os.environ.pop("OV_NAV_PATCH_DUMP", None)


def test_guard_rejects_changed_source():
    fake = types.SimpleNamespace(
        SemanticProcessor=type(
            "SemanticProcessor", (), {"_generate_overview": lambda self: None}
        )
    )
    assert nav.apply(fake) is False


def test_sitecustomize_hook_patched_on_import():
    # sitecustomize is on PYTHONPATH in the test run, exactly as in the pod, so the
    # import at the top of this file must already have been patched by the hook.
    assert getattr(sp.SemanticProcessor._generate_overview, "_ov_nav_patch", False)


def test_guard_accepts_installed_v0420_and_wraps():
    current = sp.SemanticProcessor._generate_overview
    orig = getattr(current, "__wrapped__", current)  # undo the import-time hook
    sp.SemanticProcessor._generate_overview = orig
    try:
        assert nav.apply(sp) is True
        assert sp.SemanticProcessor._generate_overview.__wrapped__ is orig
        assert nav.apply(sp) is True  # idempotent: no double wrap
        assert sp.SemanticProcessor._generate_overview.__wrapped__ is orig
    finally:
        sp.SemanticProcessor._generate_overview = current


def test_wrapper_end_to_end_with_stubbed_model():
    import asyncio
    import os

    fs, cs = files(7), children(2)
    orig = sp.SemanticProcessor._generate_overview

    async def fake_orig(self, dir_uri, f, c, llm_sem=None, tf=None, tc=None):
        return model_output(f, c, drop_nav_tail=5)

    fake_orig.__qualname__ = orig.__qualname__
    os.environ["OV_NAV_PHASE2"] = "0"  # this test pins the phase 1 layout
    try:
        assert nav.apply(sp) is True
        wrapper = sp.SemanticProcessor._generate_overview
        # re-bind the closure's orig by applying on a shim module whose method is the stub
        shim_cls = type("SemanticProcessor", (sp.SemanticProcessor,), {})
        shim = types.SimpleNamespace(
            SemanticProcessor=shim_cls,
            get_openviking_config=lambda: types.SimpleNamespace(
                semantic=types.SimpleNamespace(overview_max_chars=CAP)
            ),
        )
        nav_src_hash = nav.EXPECTED_SOURCE_SHA256
        shim_cls._generate_overview = fake_orig
        nav.EXPECTED_SOURCE_SHA256 = nav._source_hash(fake_orig)
        assert nav.apply(shim) is True
        out = asyncio.run(shim_cls._generate_overview(P, DIR, fs, cs))
        nav.EXPECTED_SOURCE_SHA256 = nav_src_hash
        nav_block = out[
            out.index("## Quick Navigation") : out.index("## Directory Coverage")
        ]
        assert targets(fs, cs) <= links_in(nav_block)
        assert wrapper is not None
    finally:
        sp.SemanticProcessor._generate_overview = orig
        os.environ.pop("OV_NAV_PHASE2", None)


# --- Phase 2 --------------------------------------------------------------------------


def real_files(n):
    """n entries built from the real summaries, padded to real length (~1,100 chars)."""
    out = []
    for i in range(n):
        name, summary, _ = REAL_GLOSSES[i % len(REAL_GLOSSES)]
        tail = (
            " The fix changed the loader and added a regression test for the case."
            " Verification ran against staging and production data."
        ) * 6
        out.append({"name": f"{i:03d}-{name}", "summary": summary + tail})
    return out


def h3_targets(text):
    import re

    heading = re.compile(r"^### \[[^\]]*\]\((viking://[^)\s]+)\)$", re.MULTILINE)
    return set(heading.findall(text))


def test_phase2_contents_link_every_entry_and_fill_the_cache():
    fs, cs = real_files(87), children(6)
    out = nav.assemble_contents(P, "Resolved bugs.", DIR, fs, cs, None, None, CAP)
    assert len(out) <= CAP, len(out)
    assert P._truncate_generated_text(out, CAP) == out
    assert h3_targets(out) == targets(fs, cs)
    cache = P._parse_overview_md(out)
    for x in fs:
        body = cache.get(x["name"], "")
        assert len(body) >= 60, (x["name"], body)
        assert not body.lower().startswith("this document"), body
        assert "Total direct entries" not in body
    assert P._extract_abstract_from_overview(out) == "Resolved bugs."
    assert out.index("## Directory Coverage") < out.index(nav.CONTENTS_HEADING)
    assert "all of them are listed below." in out


def test_phase2_bodies_are_stable_when_fed_back_from_the_cache():
    fs, cs = real_files(87), children(6)
    first = nav.assemble_contents(P, "Brief.", DIR, fs, cs, None, None, CAP)
    cache = P._parse_overview_md(first)
    again = [{"name": x["name"], "summary": cache[x["name"]]} for x in fs]
    second = nav.assemble_contents(P, "Brief.", DIR, again, cs, None, None, CAP)
    assert second == first


def test_phase2_entry_body_is_idempotent_on_real_summaries():
    for name, summary, _ in REAL_GLOSSES + OPEN_ITEM_GLOSSES:
        for budget in (60, 120, 250, 500):
            once = nav.entry_body(summary, budget, name)
            assert len(once) <= budget, (budget, once)
            assert nav.entry_body(once, budget, name) == once, (budget, once)
            assert not once or once.split()[-1].lower().rstrip(".") not in nav.DANGLING


def test_phase2_body_drops_sentences_about_the_document_layout():
    summary = (
        "This document is a bug report and resolution log for BUG-1001, which details an "
        "incorrect refresh path in the OpenViking system. The file covers the problem "
        "description, root cause analysis, investigation notes, and the fix. The refresh "
        "now reads the canonical revision watermark."
    )
    body = nav.entry_body(summary, 500, "bug-1001.md")
    assert body == (
        "Incorrect refresh path in the OpenViking system. "
        "The refresh now reads the canonical revision watermark."
    ), body


def test_phase2_small_directory_keeps_whole_summaries_up_to_the_body_cap():
    fs = real_files(3)
    out = nav.assemble_contents(P, "Brief.", DIR, fs, [], None, None, CAP)
    cache = P._parse_overview_md(out)
    assert all(
        nav.MAX_BODY_CHARS - 80 <= len(b) <= nav.MAX_BODY_CHARS for b in cache.values()
    ), [len(b) for b in cache.values()]


def test_phase2_sampled_directory_states_the_gap():
    fs, cs = real_files(10), children(2)
    out = nav.assemble_contents(P, "Brief.", DIR, fs, cs, 150, 5, CAP)
    assert "12 are listed below and 143 were not individually examined" in out


def test_phase2_long_names_never_rely_on_the_stock_cut():
    # Codex P2: 105 long names made the headings alone exceed the cap; the stock
    # size limit then cut every link while coverage still said "all listed below".
    import re

    long = [
        {"name": f"{i:03d}-" + "very-long-entry-name-" * 8 + ".md", "summary": "S."}
        for i in range(105)
    ]
    out = nav.assemble_contents(P, "Brief.", DIR, long, [], None, None, CAP)
    assert len(out) <= CAP, len(out)
    assert P._truncate_generated_text(out, CAP) == out
    links = re.findall(r"\]\((viking://[^)\s]+)\)", out)
    m = re.search(
        r"Total direct entries: 105 \(105 files, 0 subdirectories\); (.+)", out
    )
    assert links and m, out[:300]
    stated = m.group(1)
    if len(links) == 105:
        assert stated.startswith("all of them are listed below"), stated
    else:
        assert stated.startswith(f"{len(links)} are listed below"), (len(links), stated)


def test_phase2_contents_tiers_degrade_in_order():
    fs = files(5)
    headings = [h for h, _, _ in nav._contents_entries(P, DIR, fs, [])]
    headings_only = "\n\n".join([nav.CONTENTS_HEADING, *headings])
    full, n = nav.build_contents(P, DIR, fs, [], 5000)
    assert n == 5 and full.count("### [") == 5 and len(full) > len(headings_only)
    bare, n = nav.build_contents(P, DIR, fs, [], len(headings_only))
    assert (n, bare) == (5, headings_only), bare
    bullet = len(headings[0].removeprefix("### ")) + 2
    tight = len(nav.CONTENTS_HEADING) + 2 + 2 * (bullet + 1)
    cut, n = nav.build_contents(P, DIR, fs, [], tight)
    assert n == 2 and len(cut) <= tight and cut.count("- [") == 2, (n, cut)


def test_phase2_brief_is_sanitized_to_prose():
    raw = (
        "# resolved\n\n## Brief Description\n\n"
        "**Brief Description:** Resolved bugs across repos, with fixes.\n"
        "- a stray bullet\n| a | b |\n"
        "They share root causes in [the loader](viking://resources/x/l).\n\n"
        "## Quick Navigation\n\n- [bug-1.md](viking://resources/x/bug-1.md) — gloss.\n\n"
        "Prose the model wrote after a later heading."
    )
    out = nav.assemble_contents(P, raw, DIR, files(2), [], None, None, CAP)
    abstract = P._extract_abstract_from_overview(out)
    assert abstract.startswith("Resolved bugs across repos, with fixes."), abstract
    head = out[: out.index("## Directory Coverage")]
    assert "viking://" not in head and "|" not in head and "**" not in head, head
    assert "stray bullet" not in head and "the loader" in head, head
    assert "later heading" not in out, "prose after a heading is not brief"
    assert out.startswith("# resolved\n\n")


def test_phase2_failed_or_cut_off_brief_gets_code_brief():
    for raw in ("", "[Directory overview is not generated]", "Resolved bugs across"):
        out = nav.assemble_contents(P, raw, DIR, files(3), children(1), None, None, CAP)
        abstract = P._extract_abstract_from_overview(out)
        assert abstract.startswith("Directory resolved with 3 files"), (raw, abstract)


def test_phase2_scope_is_resources_only():
    import os

    assert nav.phase2_enabled("viking://resources/compendium/bugs")
    assert not nav.phase2_enabled("viking://user/noot-pilot/memories/events")
    os.environ["OV_NAV_PHASE2"] = "0"
    try:
        assert not nav.phase2_enabled("viking://resources/compendium/bugs")
    finally:
        os.environ.pop("OV_NAV_PHASE2", None)


def test_phase2_self_check_passes_on_installed_v0420():
    assert nav.phase2_self_check(sp.SemanticProcessor)


def test_phase2_wrapper_calls_model_for_brief_only_with_max_tokens():
    import asyncio

    fs, cs = real_files(12), children(2)
    calls = []

    class FakeVLM:
        def is_available(self):
            return True

        async def get_completion_async(self, prompt="", max_tokens=None):
            calls.append((prompt, max_tokens))
            return "# ignored\n\nResolved bugs across repos.\n\n## Detailed Description\n\n### [x](y)\n\nz."

    async def stock(self, dir_uri, f, c, llm_sem=None, tf=None, tc=None):
        raise AssertionError("phase 2 must not call the stock generator")

    shim_cls = type("SemanticProcessor", (sp.SemanticProcessor,), {})
    shim_cls._generate_overview = stock
    shim = types.SimpleNamespace(
        SemanticProcessor=shim_cls,
        get_openviking_config=lambda: types.SimpleNamespace(
            vlm=FakeVLM(), semantic=types.SimpleNamespace(overview_max_chars=CAP)
        ),
    )
    saved = nav.EXPECTED_SOURCE_SHA256
    nav.EXPECTED_SOURCE_SHA256 = nav._source_hash(stock)
    try:
        assert nav.apply(shim) is True
        out = asyncio.run(shim_cls._generate_overview(P, DIR, fs, cs))
    finally:
        nav.EXPECTED_SOURCE_SHA256 = saved
    assert len(calls) == 1 and calls[0][1] == nav.BRIEF_MAX_TOKENS
    prompt = calls[0][0]
    assert "Verification ran against staging" not in prompt, "full summaries leaked"
    assert all(x["name"] in prompt for x in fs + cs)
    assert h3_targets(out) == targets(fs, cs)
    assert P._extract_abstract_from_overview(out) == "Resolved bugs across repos."
    assert "### [x](y)" not in out


if __name__ == "__main__":
    failed = 0
    tests = [
        (n, f)
        for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
