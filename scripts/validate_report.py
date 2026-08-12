#!/usr/bin/env python3
"""
validate_report.py -- the delivery gate for the finished report. MANUAL §16.5.

Two jobs, both mechanical:

  COMPLETENESS  Does the report actually cover what the run found? Every core
                node in the bibliography, every cluster discussed, every
                component adjudicated, every opportunity listed. A rule nobody
                checks is a rule that gets broken by hour eleven.

  READABILITY   Does it obey §16? Verdict page under 600 words with no
                subsections, sentences under 40 words, no banned filler, one
                name per concept, no forward references.

The two pull in opposite directions on purpose. Completeness lives in the
reference sections; brevity lives in the reading path. A report fails if it
solved a length problem by deleting evidence, and equally if it buried the
decision under bulk.

Usage:
    python3 validate_report.py --run-root ./runs/<slug>
    python3 validate_report.py --run-root ./runs/<slug> --json

Exit codes:  0 clean · 1 defects · 2 could not run
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

MAX_VERDICT_WORDS = 600
MAX_SENTENCE_WORDS = 40
TARGET_MEAN_SENTENCE = 20
MAX_ARGUMENT_WORDS = 6000

BANNED = [
    (r"\bit is worth noting\b", "it is worth noting"),
    (r"\bit'?s worth noting\b", "it's worth noting"),
    (r"\bit is important to (note|remember|understand)\b", "it is important to…"),
    (r"\bdelv(e|es|ing)\b", "delve"),
    (r"\blandscape\b", "landscape"),
    (r"\bleverag(e|es|ing)\b", "leverage (as a verb)"),
    (r"\butiliz(e|es|ing|ation)\b", "utilize"),
    (r"\ba rich (body|literature|tradition)\b", "a rich body of work"),
    (r"\brapidly evolving\b", "rapidly evolving"),
    (r"\bin recent years,\b", "in recent years,"),
    (r"\bmay potentially\b", "stacked hedge: may potentially"),
    (r"\bcould possibly\b", "stacked hedge: could possibly"),
    (r"\bmight perhaps\b", "stacked hedge: might perhaps"),
    (r"\bsomewhat (may|might|could)\b", "stacked hedge"),
    (r"\bas we (will|shall) see\b", "forward reference"),
    (r"\b(see|discussed) (below|later)\b", "forward reference"),
]

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
CODE_FENCE = re.compile(r"```.*?```", re.S)
TABLE_ROW = re.compile(r"^\s*\|")
HEADING_TEXT = re.compile(r"^#+\s*")
BULLET = re.compile(r"^([-*+]|\d+\.)\s+")


class Defects:
    def __init__(self):
        self.items = defaultdict(list)

    def add(self, kind, subject, detail):
        self.items[kind].append({"subject": subject, "detail": detail})

    def blocking(self):
        return {k: v for k, v in self.items.items() if not k.startswith("info_")}

    def n_blocking(self):
        return sum(len(v) for v in self.blocking().values())


def split_sections(md):
    """Return {section_number: (heading, body)} for top-level '## N.' headings."""
    out, cur, buf = {}, None, []
    for line in md.splitlines():
        m = re.match(r"^##\s+(\d+)\.\s*(.*)", line)
        if m:
            if cur is not None:
                out[cur[0]] = (cur[1], "\n".join(buf))
            cur, buf = (int(m.group(1)), m.group(2).strip()), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur[0]] = (cur[1], "\n".join(buf))
    return out


def prose_only(text):
    """Strip code fences and table rows, and terminate headings and list items.

    Neither fences nor tables are prose. Headings are not sentences either, but
    they must still act as terminators -- without that, the splitter runs the
    heading into the paragraph below it and reports one 140-word sentence that
    does not exist.
    """
    text = CODE_FENCE.sub(" ", text)
    out = []
    for ln in text.splitlines():
        if TABLE_ROW.match(ln):
            continue
        stripped = ln.strip()
        if stripped.startswith("#"):
            out.append(HEADING_TEXT.sub("", stripped).rstrip(" .") + ".")
        elif BULLET.match(stripped):
            body = BULLET.sub("", stripped).rstrip()
            out.append(body if body.endswith((".", "!", "?", ":")) else body + ".")
        else:
            out.append(ln)
    return "\n".join(out)


def sentences(text):
    text = re.sub(r"\s+", " ", prose_only(text))
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", "LINK", text)
    return [s.strip() for s in SENT_SPLIT.split(text) if len(s.split()) > 2]


# The run-state contract lives in state_io.py, copied into every run beside
# this script. No literal fallbacks: duplicated definitions are what drifted.
try:
    from state_io import (NOVELTY_MODES, load_mode, read_json_object,
                          id_present)
except ImportError:
    sys.exit("[validate_report] state_io.py must sit beside this script "
             "(init_run.py copies it into the run).")


def load_json(path, default=None):
    """Objects only; a corrupt or wrong-typed state file returns the default
    AND is reported by the caller — never silently treated as absent."""
    doc, problems = read_json_object(path)
    if problems:
        return default, problems
    return (doc or default), []


def check_readability(md, sections, d, mode="fresh"):
    # --- §16.3 the verdict page. Form rules (length, structure, sentences)
    # are universal. The recommendation-word and falsifier checks encode the
    # NOVELTY verdict's shape and apply only to the modes that produce one —
    # retrospective/concept/problem §0 formats (§23.4-§23.6) have their own
    # mode checks in check_completeness.
    novelty_verdict = mode in NOVELTY_MODES
    if 0 not in sections:
        d.add("no_verdict_section", "§0", "report has no '## 0.' verdict section")
    else:
        _, body = sections[0]
        words = len(prose_only(body).split())
        if words > MAX_VERDICT_WORDS:
            d.add("verdict_too_long", "§0",
                  f"{words} words (max {MAX_VERDICT_WORDS}) -- this is the only "
                  "part guaranteed to be read")
        if re.search(r"^###\s", body, re.M):
            d.add("verdict_has_subsections", "§0",
                  "§0 must have no subsections (§16.3)")
        head = " ".join(prose_only(body).split()[:60]).lower()
        if novelty_verdict and not re.search(
                r"\b(proceed|pivot|abandon|reframe|reframing)\b", head):
            d.add("verdict_no_recommendation", "§0",
                  "first 60 words state no recommendation "
                  "(proceed / reframe / pivot / abandon)")
        if novelty_verdict and not re.search(
                r"\bwould change\b|\bchange this verdict\b", body, re.I):
            d.add("verdict_no_falsifier", "§0",
                  "§0 must say what would change the verdict")
        if not re.search(r"\bcorpus\b|\bcoverage\b|\bthin\b|\bwith respect to\b",
                         body, re.I):
            d.add("verdict_unconditional_confidence", "§0",
                  "confidence must be stated conditional on coverage (§13.2)")

    # --- §16.1 the argument layer
    arg = " ".join(prose_only(sections[i][1]) for i in range(1, 8) if i in sections)
    if len(arg.split()) > MAX_ARGUMENT_WORDS:
        d.add("info_argument_long", "§§1-7",
              f"{len(arg.split())} words (soft cap {MAX_ARGUMENT_WORDS})")

    # --- §16.2 sentence discipline, reading path only
    reading = "\n".join(sections[i][1] for i in range(0, 8) if i in sections)
    sents = sentences(reading)
    if sents:
        long_ones = [s for s in sents if len(s.split()) > MAX_SENTENCE_WORDS]
        for s in long_ones[:12]:
            d.add("sentence_too_long", f"{len(s.split())} words", s[:150] + "…")
        mean = sum(len(s.split()) for s in sents) / len(sents)
        if mean > TARGET_MEAN_SENTENCE + 4:
            d.add("mean_sentence_long", "§§0-7",
                  f"mean {mean:.1f} words (target ≤{TARGET_MEAN_SENTENCE})")

    # --- banned filler, whole document
    body_all = prose_only(md)
    for pattern, label in BANNED:
        hits = re.findall(pattern, body_all, re.I)
        if hits:
            d.add("banned_phrase", label, f"{len(hits)} occurrence(s)")

    # --- §16.2 heading depth in the reading path
    for i in range(0, 8):
        if i in sections and re.search(r"^####\s", sections[i][1], re.M):
            d.add("heading_too_deep", f"§{i}",
                  "reading path must not nest past '###'")


def _require_subsection(md, heading_re, where, kind, why, d):
    """A §23 mode subsection must exist and hold real content -- a bare
    heading is the checkbox version of the requirement."""
    m = re.search(heading_re, md, re.M | re.I)
    if not m:
        d.add(kind, where, why)
        return
    tail = md[m.end():]
    nxt = re.search(r"^#{1,3}\s", tail, re.M)
    body = tail[:nxt.start()] if nxt else tail
    if len(prose_only(body).split()) < 30:
        d.add(kind, where,
              f"subsection present but under 30 words -- {why}")


def check_completeness(md, sections, root, d, mode="fresh", mode_doc=None):
    mode_doc = mode_doc or {}
    graph = None
    for cand in (os.path.join(root, "out", "graph.json"),
                 os.path.join(root, "graph", "graph.json")):
        if os.path.exists(cand):
            graph, gp = load_json(cand)
            for problem in gp:
                d.add("graph_unreadable", cand, problem)
            break
    if graph is None:
        d.add("no_graph", root, "cannot verify completeness without a graph")
        return

    # every core node must appear in the bibliography (§15.4)
    core = [n["id"] for n in graph.get("nodes", []) if n.get("status") == "core"]
    bib = sections.get(9, ("", ""))[1] + sections.get(10, ("", ""))[1]
    missing = [nid for nid in core if not id_present(nid, bib)]
    if missing:
        d.add("bibliography_incomplete", f"{len(missing)} of {len(core)} core nodes",
              "missing: " + ", ".join(missing[:10]) +
              (" …" if len(missing) > 10 else "") +
              "  -- move evidence out of the reading path, never delete it (§16)")

    # every cluster must be discussed (§15.4)
    lit = sections.get(5, ("", ""))[1]
    for c in graph.get("clusters", []) or []:
        label, cid = (c.get("label") or "").strip(), c.get("id", "")
        if label and label not in lit and not id_present(cid, lit):
            d.add("cluster_not_discussed", cid,
                  f"thread '{label}' identified by the graph but absent from §5")
        if re.fullmatch(r"cluster[_\-]?\d+", label or "", re.I):
            d.add("cluster_numeric_label", cid,
                  f"label '{label}' is an index, not a name (§16.4)")
        if not (c.get("narrative") or "").strip():
            d.add("cluster_no_narrative", cid, "thread has no narrative")

    # every component adjudicated (§13)
    nov = sections.get(2, ("", ""))[1]
    for comp in graph.get("meta", {}).get("components", []):
        if not id_present(comp, nov):
            d.add("component_not_adjudicated", comp, "absent from §2")

    # every opportunity listed (§15.4)
    opp_path = os.path.join(root, "opportunities", "opportunities.jsonl")
    if os.path.exists(opp_path):
        nxt = sections.get(7, ("", ""))[1]
        with open(opp_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("id") and not id_present(o["id"], nxt):
                    d.add("opportunity_not_reported", o["id"], "absent from §7")
                if not (o.get("why_now") or "").strip():
                    d.add("opportunity_no_why_now", o.get("id", "?"),
                          "recorded without why_now (§11.2)")
                if not (o.get("falsifier") or "").strip():
                    d.add("opportunity_no_falsifier", o.get("id", "?"),
                          "recorded without a searched falsifier")

    # the anti-report (§15.3)
    if 10 not in sections and "would make this report wrong" not in md.lower():
        d.add("no_anti_report", "§10", "'What would make this report wrong' missing")

    # the recall check made it into the report (§0.1, §13.2). The record is
    # required in EVERY run: from the sealed file, from a pasted list
    # (--paste), or an explicit none-provided record (--none). A run without
    # the record is a run whose only external calibration is unaccounted for.
    rc_path = os.path.join(root, "state", "recall_check.json")
    if not os.path.exists(rc_path):
        d.add("recall_never_computed", "P4",
              "state/recall_check.json missing -- at P4 run "
              "scripts/recall_check.py (sealed file), --paste <file> "
              "(operator-held list), or --none (no list exists) (§0.1)")
    else:
        rc, rcp = load_json(rc_path, {})
        rc = rc or {}
        for problem in rcp:
            d.add("recall_check_unreadable", rc_path, problem)
        coverage = sections.get(8, ("", ""))[1]
        verdict = sections.get(0, ("", ""))[1]
        if rc.get("n_missed", 0) > 0:
            if "recall" not in coverage.lower():
                d.add("recall_miss_not_reported", "§8",
                      f"{rc['n_missed']} sealed item(s) missed but §8 "
                      "never mentions the recall check (§22)")
            if "recall" not in verdict.lower():
                d.add("recall_miss_not_in_verdict", "§0",
                      "a recall miss lowers every confidence and §0 must "
                      "say so explicitly (§13.2)")
        if not rc.get("total") and "calibration" not in coverage.lower():
            d.add("recall_absence_not_disclosed", "§8",
                  "no recall list existed (source: "
                  f"{rc.get('source', 'unknown')!r}) -- §8 must state that "
                  "the run had no external calibration (§13.2)")
        if rc.get("hash_consistent") is False and \
                "sealed" not in coverage.lower():
            d.add("seal_tamper_not_reported", "§8",
                  "the sealed file changed mid-run (hash mismatch) and §8 "
                  "does not surface it (§0.1)")

    # §23 mode-conditional subsections
    if mode == "incremental":
        _require_subsection(md, r"^###\s+Impact evidence\b", "§2",
                            "impact_evidence_missing",
                            "incremental runs must report the feature's measured "
                            "effect in neighboring systems, or state that no "
                            "comparable evidence exists (§23.1)", d)
    elif mode == "anchored":
        _require_subsection(md, r"^###\s+Anchor solidity\b", "§1",
                            "anchor_solidity_missing",
                            "anchored runs must assess which anchor claims the "
                            "follow-up load-bears on and how strong each is "
                            "(§23.2)", d)
    elif mode == "retrospective":
        # §23.4 — §0 is the claim scoreboard: a table carrying every claim.
        body = sections.get(0, ("", ""))[1]
        if not re.search(r"^\s*\|.*\|", body, re.M):
            d.add("scoreboard_missing", "§0",
                  "retrospective §0 must contain the claim scoreboard table "
                  "(claim | fate | deciding papers | dates) — §23.4")
        for cl in mode_doc.get("claims") or []:
            if not id_present(cl, body):
                d.add("claim_not_in_scoreboard", cl,
                      "claim absent from the §0 scoreboard — every claim's "
                      "fate is the verdict (§23.4)")
    elif mode == "concept":
        # §23.5 — origin lineage and reading path are mandatory; the
        # disambiguation is mandatory exactly when the name has >1 sense.
        _require_subsection(md, r"^###\s+Origin\b", "§1",
                            "origin_missing",
                            "concept runs must trace the origin and pre-name "
                            "lineage (§23.5)", d)
        _require_subsection(md, r"^###\s+Reading path\b", "§7",
                            "reading_path_missing",
                            "concept runs must give the foundations → "
                            "canonical → frontier reading path (§23.5)", d)
        res, resp = load_json(os.path.join(root, "state",
                                           "concept_resolution.json"), {})
        for problem in resp:
            d.add("concept_resolution_unreadable", "state", problem)
        senses = (res or {}).get("senses") or []
        if len(senses) > 1:
            _require_subsection(md, r"^###\s+Disambiguation\b", "§1",
                                "disambiguation_missing",
                                f"the term has {len(senses)} confirmed senses "
                                "and the report must lead with the split "
                                "(§23.5)", d)
    elif mode == "problem":
        # §23.6 — §0 is the ranked recommendation; every solution reported;
        # unsolved requirements are a headline, not a footnote; the failure
        # museum has a mechanical backstop like every other mode subsection.
        body = sections.get(0, ("", ""))[1]
        sol_doc, solp = load_json(os.path.join(root, "state",
                                               "solutions.json"), {})
        for problem in solp:
            d.add("solutions_unreadable", "state/solutions.json", problem)
        sol_doc = sol_doc if isinstance(sol_doc, dict) else {}
        # Ids must be usable strings: an empty or non-string key would make
        # every downstream match vacuously true.
        sol_ids = []
        for i in (sol_doc.get("solutions") or {}):
            if isinstance(i, str) and i.strip():
                sol_ids.append(i.strip())
            else:
                d.add("solution_id_unusable", repr(i),
                      "registry key is not a non-empty string; it would "
                      "match everything in the report checks")
        # The table check is derived from the registry's ACTUAL ids — no
        # naming convention is imposed. An empty or missing registry is a
        # blocking defect, never a skipped check (fail-closed).
        if not sol_ids:
            d.add("solutions_registry_missing", "state/solutions.json",
                  "problem-mode delivery with no solutions registry -- the "
                  "§0 ranked table cannot be verified and the run has "
                  "nothing to recommend (§23.6)")
        else:
            guard = r"(?<![A-Za-z0-9_])(?:" + \
                "|".join(re.escape(i) for i in sol_ids) + r")(?![A-Za-z0-9_])"
            row_pat = re.compile(r"^\s*\|.*" + guard, re.M)
            if not row_pat.search(body):
                d.add("recommendation_missing", "§0",
                      "problem §0 must contain the ranked recommendation "
                      "table referencing the registry's solutions — §23.6")
        for sid in sol_ids:
            if not id_present(sid, md):
                d.add("solution_not_reported", sid,
                      "in the registry but absent from the report — the "
                      "failed and abandoned belong on the map too (§23.6)")
        for r in sol_doc.get("unsolved_requirements") or []:
            if not id_present(r, body):
                d.add("unsolved_not_in_verdict", r,
                      "unsolved requirement absent from §0 — that is a "
                      "headline finding (§23.6)")
        # A museum laid out as a table is a fine museum, so table cells
        # count — but code fences do not (they are not prose an operator
        # reads), and separator rows are not words.
        sec3 = sections.get(3, ("", ""))[1]
        content = CODE_FENCE.sub(" ", sec3)
        content = re.sub(r"^#+.*$", " ", content, flags=re.M)
        content = re.sub(r"^\s*\|?[-\s|:]+\|?\s*$", " ", content, flags=re.M)
        words = len(content.replace("|", " ").split())
        if words < 30:
            d.add("failure_museum_missing", "§3",
                  "problem runs must carry the failure museum — what was "
                  "tried and abandoned, with the stated reasons; if nothing "
                  "failed, §3 says that and what it implies (§23.6)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--report", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    path = args.report or os.path.join(args.run_root, "out", "report.md")
    if not os.path.exists(path):
        print(f"[validate_report] FATAL: no report at {path}", file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as fh:
        md = fh.read()

    d = Defects()
    sections = split_sections(md)
    mode, mode_doc, mode_problem = load_mode(args.run_root)
    if mode_problem:
        d.add("mode_invalid", "state/mode.json",
              mode_problem + "  Checks ran as fresh; fix mode.json rather "
              "than working around it.")
    check_readability(md, sections, d, mode)
    check_completeness(md, sections, args.run_root, d, mode, mode_doc)

    n = d.n_blocking()
    summary = {"report": path, "sections_found": sorted(sections),
               "words": len(prose_only(md).split()),
               "blocking": n, "informational": sum(len(v) for v in d.items.values()) - n,
               "verdict": "PASS" if n == 0 else "FAIL"}

    if args.json:
        print(json.dumps({"summary": summary, "defects": dict(d.items)},
                         indent=2, ensure_ascii=False))
    else:
        print(f"\n=== validate_report: {summary['verdict']} ===")
        print(f"  {summary['words']} words · sections {summary['sections_found']}")
        print(f"  blocking {n} · informational {summary['informational']}\n")
        for kind, items in sorted(d.items.items()):
            mark = "i" if kind.startswith("info_") else "!"
            print(f" [{mark}] {kind}  ({len(items)})")
            for it in items[:6]:
                print(f"       {it['subject']}: {it['detail']}")
            if len(items) > 6:
                print(f"       ... and {len(items)-6} more")
        if n:
            print("\n  Delivery is BLOCKED until these are resolved "
                  "(MANUAL §16.5, §22).\n")
    return 0 if n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
