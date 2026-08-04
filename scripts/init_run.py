#!/usr/bin/env python3
"""
init_run.py -- scaffold a run and find out what this machine can actually do.

Run this once before starting. It does two jobs:

  SCAFFOLD   Creates the full directory tree from MANUAL.md §3 and drops in the
             operator templates, so nothing has to be improvised at startup.

  PREFLIGHT  Probes what is genuinely available -- PDF tooling, python
             libraries, and which scholarly endpoints respond -- and writes the
             result to state/capabilities.json. The manual is written to
             degrade gracefully, but only if it knows what is missing. Guessing
             at capability and discovering the truth at hour six is the
             expensive way to find out.

Usage:
    python3 scripts/init_run.py --slug my-idea
    python3 scripts/init_run.py --slug my-idea --no-probe
    python3 scripts/init_run.py --slug my-idea --force

Run modes (MANUAL §23) — the flags below write state/mode.json, which
graph_metrics.py, round.py, and validate_report.py read:

    # incremental: delta against a completed prior run (imports its corpus)
    python3 scripts/init_run.py --slug my-idea-delta --base-run runs/my-idea

    # fresh gates, but seed the corpus from a prior run
    python3 scripts/init_run.py --slug my-idea-2 --base-run runs/my-idea --seed-only

    # anchored: follow-up idea to a named artifact
    python3 scripts/init_run.py --slug follow-up --anchor https://arxiv.org/abs/xxxx --anchor-kind paper

    # retrospective: what happened to this paper — claims, descent, evolution
    python3 scripts/init_run.py --slug attention-retro --subject https://arxiv.org/abs/1706.03762
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DIRS = [
    "state", "state/fetch_cache",
    "corpus", "corpus/pdf", "corpus/text", "corpus/cards",
    "graph", "graph/snapshots",
    "redteam", "redteam/dossiers",
    "opportunities",
    "scripts", "out", "out/adjudication_passes",
]

# (binary, why it matters, whether the run degrades badly without it)
BINARIES = [
    ("pdftotext", "primary text extraction; -layout handles 2-column papers", True),
    ("pdffonts", "detects scanned PDFs with no text layer", True),
    ("pdfinfo", "page count and metadata", False),
    ("pdftoppm", "rasterizes pages for figures and equations", False),
    ("pdfimages", "extracts embedded raster figures", False),
    ("tesseract", "OCR fallback for scanned documents", False),
    ("git", "optional: dating repositories by first commit", False),
]

MODULES = [
    ("networkx", "graph metrics and community detection", True),
    ("scipy", "pagerank backend for networkx>=3; degrades to degree centrality", False),
    ("pdfplumber", "table extraction from results sections", False),
    ("fitz", "PyMuPDF: image extraction with position data", False),
    ("pypdf", "fallback text extraction", False),
]

# Keyless endpoints. If any respond, real citation traversal becomes possible
# and the run is substantially stronger (MANUAL §2.3).
ENDPOINTS = [
    ("arxiv", "http://export.arxiv.org/api/query?search_query=all:electron&max_results=1"),
    ("openalex", "https://api.openalex.org/works?per-page=1"),
    ("semanticscholar",
     "https://api.semanticscholar.org/graph/v1/paper/search?query=attention&limit=1"),
    ("crossref", "https://api.crossref.org/works?rows=1"),
]

EMPTY_GRAPH = {
    "meta": {"idea_slug": "", "round": 0, "components": [],
             "generated": "", "manual_version": "1.2"},
    "nodes": [], "edges": [], "clusters": [],
}


# MANUAL §23.1 — incremental floors. Written into mode.json so the agent can
# read them but never silently recalibrate them (banned behavior 44).
INCREMENTAL_FLOORS = {
    "min_rounds": 6,
    "min_delta_digested": 75,
    "strategy_exhaustion_min": 8,
    "prospector_start_round": 4,
}

ANCHOR_KINDS = ("paper", "repo", "techreport", "thesis", "blogpost")


def have_binary(name):
    return shutil.which(name) is not None


def link_or_copy(src, dst):
    """Hardlink where possible (PDF corpora run to gigabytes), else copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def import_base(run, base):
    """Import a completed run's evidence per MANUAL §23.1.

    Cards, text, PDFs, index, graph, threats, and the base decomposition come
    over — all stamped inherited_from_base with round_added reset to 0.
    seen_queries / card_audits / ledger stay behind on purpose: the gates are
    proof-of-work meters for THIS run. Opportunities land in inherited.jsonl,
    reopenable only after their falsifier is re-searched.
    """
    counts = {"cards": 0, "text": 0, "pdf": 0, "nodes": 0,
              "threats": 0, "opportunities": 0}

    src_cards = os.path.join(base, "corpus", "cards")
    if os.path.isdir(src_cards):
        for name in sorted(os.listdir(src_cards)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(src_cards, name), encoding="utf-8") as fh:
                try:
                    card = json.load(fh)
                except json.JSONDecodeError:
                    continue
            card["inherited_from_base"] = True
            card["base_round_added"] = card.get("round_added")
            card["round_added"] = 0
            with open(os.path.join(run, "corpus", "cards", name), "w",
                      encoding="utf-8") as fh:
                json.dump(card, fh, indent=1, ensure_ascii=False)
            counts["cards"] += 1

    for sub, key in (("text", "text"), ("pdf", "pdf")):
        src = os.path.join(base, "corpus", sub)
        if os.path.isdir(src):
            for name in sorted(os.listdir(src)):
                dst = os.path.join(run, "corpus", sub, name)
                if not os.path.exists(dst):
                    link_or_copy(os.path.join(src, name), dst)
                    counts[key] += 1

    src_index = os.path.join(base, "corpus", "index.jsonl")
    if os.path.exists(src_index):
        shutil.copy2(src_index, os.path.join(run, "corpus", "index.jsonl"))

    base_graph = None
    for cand in (os.path.join(base, "out", "graph.json"),
                 os.path.join(base, "graph", "graph.json")):
        if os.path.exists(cand):
            with open(cand, encoding="utf-8") as fh:
                base_graph = json.load(fh)
            break
    base_report_date = None
    if base_graph:
        base_report_date = (base_graph.get("meta") or {}).get("generated")
        for node in base_graph.get("nodes", []):
            node["inherited_from_base"] = True
            node["base_round_added"] = node.get("round_added")
            node["round_added"] = 0
        meta = base_graph.setdefault("meta", {})
        meta["round"] = 0
        meta["base_run"] = os.path.abspath(base)
        counts["nodes"] = len(base_graph.get("nodes", []))
        for dst in (os.path.join(run, "graph", "graph.json"),
                    os.path.join(run, "graph", "snapshots", "round_00.json")):
            with open(dst, "w", encoding="utf-8") as fh:
                json.dump(base_graph, fh, indent=2, ensure_ascii=False)

    src_threats = os.path.join(base, "redteam", "threats.jsonl")
    if os.path.exists(src_threats):
        with open(src_threats, encoding="utf-8") as fh, \
             open(os.path.join(run, "redteam", "threats.jsonl"), "w",
                  encoding="utf-8") as out:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t["inherited_from_base"] = True
                t["base_round_added"] = t.get("round_added")
                t["round_added"] = 0
                out.write(json.dumps(t, ensure_ascii=False) + "\n")
                counts["threats"] += 1

    inherited_opps = os.path.join(run, "opportunities", "inherited.jsonl")
    with open(inherited_opps, "w", encoding="utf-8") as out:
        for name in ("opportunities.jsonl", "closed.jsonl"):
            src = os.path.join(base, "opportunities", name)
            if not os.path.exists(src):
                continue
            with open(src, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    o["inherited_from_base"] = True
                    o["needs_falsifier_recheck"] = True
                    o["was_in"] = name
                    out.write(json.dumps(o, ensure_ascii=False) + "\n")
                    counts["opportunities"] += 1

    src_decomp = os.path.join(base, "state", "decomposition.json")
    if os.path.exists(src_decomp):
        shutil.copy2(src_decomp,
                     os.path.join(run, "state", "base_decomposition.json"))

    return counts, base_report_date


def have_module(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def probe(url, timeout=12):
    """Usable, not merely responsive.

    The question is "can I get data from this", not "did something answer".
    A corporate egress proxy returns a perfectly well-formed 403 for every
    blocked domain -- treating that as reachable tells the agent to rely on
    citation traversal it does not have, which is worse than knowing it is
    blocked. So: require 2xx AND a body that parses as the expected format.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "deep-research-agent/1.2 (academic literature review; "
                      "contact: set CONTACT_EMAIL)",
        "Accept": "application/json,application/atom+xml,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            body = r.read(4096).decode("utf-8", "replace").lstrip()
        if not (200 <= status < 300):
            return {"usable": False, "status": status, "note": "non-2xx"}
        looks_json = body.startswith(("{", "["))
        looks_feed = body.startswith("<?xml") or "<feed" in body[:400]
        if looks_json or looks_feed:
            return {"usable": True, "status": status,
                    "format": "json" if looks_json else "atom"}
        return {"usable": False, "status": status,
                "note": "2xx but body is not JSON or a feed -- likely an "
                        "interstitial or proxy page"}
    except urllib.error.HTTPError as e:
        hint = ""
        try:
            hint = (e.headers.get("x-deny-reason") or "").strip()
        except Exception:
            pass
        note = f"blocked by network policy ({hint})" if hint else (
            "rate limited -- host is reachable" if e.code == 429
            else "rejected; may be policy or may be the query")
        return {"usable": e.code == 429, "status": e.code, "note": note}
    except Exception as e:
        return {"usable": False, "status": None, "note": type(e).__name__}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True, help="short name for this run")
    ap.add_argument("--root", default="runs")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip network probes (offline setup)")
    ap.add_argument("--force", action="store_true",
                    help="scaffold into a directory that already exists")
    ap.add_argument("--base-run", default=None,
                    help="path to a completed run; imports its corpus and sets "
                         "mode: incremental (MANUAL §23.1)")
    ap.add_argument("--seed-only", action="store_true",
                    help="with --base-run: import the corpus but keep mode: "
                         "fresh with full floors (MANUAL §23.3)")
    ap.add_argument("--anchor", default=None,
                    help="URL of the anchor artifact; sets mode: anchored "
                         "(MANUAL §23.2)")
    ap.add_argument("--anchor-kind", default="paper", choices=ANCHOR_KINDS)
    ap.add_argument("--subject", default=None,
                    help="URL of the paper to run a retrospective on; sets "
                         "mode: retrospective (MANUAL §23.4)")
    ap.add_argument("--subject-kind", default="paper", choices=ANCHOR_KINDS)
    args = ap.parse_args()

    if sum(bool(x) for x in (args.base_run, args.anchor, args.subject)) > 1:
        sys.exit("[init] --base-run, --anchor, and --subject are mutually "
                 "exclusive; pick one mode (MANUAL §23.3).")
    if args.seed_only and not args.base_run:
        sys.exit("[init] --seed-only requires --base-run.")
    if args.base_run and not os.path.isdir(args.base_run):
        sys.exit(f"[init] --base-run {args.base_run} is not a directory.")

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = os.path.join(args.root, args.slug)

    if os.path.exists(run) and not args.force:
        sys.exit(f"[init] {run} already exists. Use --force to add missing pieces, "
                 f"or pick another slug. Never scaffold over a live run.")

    for d in DIRS:
        os.makedirs(os.path.join(run, d), exist_ok=True)

    # ---- run mode (MANUAL §23)
    now = datetime.now(timezone.utc).isoformat()
    imported, base_report_date = None, None
    if args.base_run:
        print(f"importing base run from {args.base_run} (MANUAL §23.1)...")
        imported, base_report_date = import_base(run, args.base_run)
        print(f"  {imported['cards']} cards, {imported['nodes']} graph nodes, "
              f"{imported['text']} text files, {imported['pdf']} PDFs, "
              f"{imported['threats']} threats, "
              f"{imported['opportunities']} opportunities -> inherited.jsonl")
        print("  NOT imported (per §23.1): seen_queries, card_audits, ledger — "
              "this run's gates measure this run's work.")

    if args.base_run and not args.seed_only:
        mode_doc = {
            "mode": "incremental",
            "declared": now,
            "base_run": os.path.abspath(args.base_run),
            "base_report_date": base_report_date,
            "imported": imported,
            "delta_components": [],
            "floors": dict(INCREMENTAL_FLOORS),
            "note": "Agent fills delta_components after P0. mode, base_run, "
                    "and floors are operator-owned (MANUAL §23.0, banned 44).",
        }
    elif args.anchor:
        mode_doc = {
            "mode": "anchored",
            "declared": now,
            "anchor": {"ref": args.anchor, "kind": args.anchor_kind,
                       "card_id": None},
            "note": "Agent fills anchor.card_id after the anchor dossier "
                    "(MANUAL §23.2). mode is operator-owned (§23.0).",
        }
    elif args.subject:
        mode_doc = {
            "mode": "retrospective",
            "declared": now,
            "subject": {"ref": args.subject, "kind": args.subject_kind,
                        "card_id": None},
            "claims": [],
            "note": "Agent fills subject.card_id after the subject dossier and "
                    "claims after P0 (MANUAL §23.4). mode is operator-owned "
                    "(§23.0).",
        }
        os.makedirs(os.path.join(run, "state", "descent"), exist_ok=True)
    else:
        mode_doc = {"mode": "fresh", "declared": now}
        if args.seed_only:
            mode_doc["seeded_from"] = os.path.abspath(args.base_run)
            mode_doc["note"] = ("Corpus seeded from a prior run; gates run at "
                                "full fresh floors (MANUAL §23.3).")
    with open(os.path.join(run, "state", "mode.json"), "w",
              encoding="utf-8") as fh:
        json.dump(mode_doc, fh, indent=2)

    # copy the scripts in so the run is self-contained and reproducible
    src = os.path.join(here, "scripts")
    for name in sorted(os.listdir(src)):
        if name.endswith(".py") and name != "init_run.py":
            shutil.copy2(os.path.join(src, name), os.path.join(run, "scripts", name))

    # templates the operator fills in
    tpl = os.path.join(here, "templates")
    placed = []
    for name, dest in [("IDEA_BRIEF.md", "00_brief.md"),
                       ("SEALED_recall_check.md", "SEALED_recall_check.md"),
                       ("operator_notes.md", "state/operator_notes.md")]:
        s, t = os.path.join(tpl, name), os.path.join(run, dest)
        if os.path.exists(s) and not os.path.exists(t):
            shutil.copy2(s, t)
            placed.append(dest)

    graph_path = os.path.join(run, "graph", "graph.json")
    if not os.path.exists(graph_path):
        g = dict(EMPTY_GRAPH)
        g["meta"] = dict(g["meta"])
        g["meta"]["idea_slug"] = args.slug
        g["meta"]["generated"] = datetime.now(timezone.utc).isoformat()
        with open(graph_path, "w", encoding="utf-8") as fh:
            json.dump(g, fh, indent=2)

    for f in ["state/ledger.jsonl", "state/seen_queries.jsonl",
              "state/anomalies.jsonl", "state/card_audits.jsonl",
              "redteam/threats.jsonl", "opportunities/opportunities.jsonl",
              "opportunities/closed.jsonl", "corpus/index.jsonl"]:
        p = os.path.join(run, f)
        if not os.path.exists(p):
            open(p, "a").close()

    caps = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "manual_version": "1.2",
        "run_root": os.path.abspath(run),
        "binaries": {n: {"present": have_binary(n), "why": why, "critical": crit}
                     for n, why, crit in BINARIES},
        "modules": {n: {"present": have_module(n), "why": why, "critical": crit}
                    for n, why, crit in MODULES},
        "endpoints": {},
        "tool_mapping": {
            "SEARCH": "TODO: name your harness's web search tool",
            "FETCH": "TODO: name your harness's fetch tool",
            "SHELL": "TODO",
            "READ": "TODO",
            "WRITE": "TODO",
            "SPAWN": "TODO -- if unavailable, MANUAL §2.1 degradation applies",
            "VISION": "TODO -- optional",
        },
        "max_concurrency_configured": 10,
        "max_concurrency_achieved": None,
        "worker_config": {
            "context_window": "1M — set explicitly on every subagent (MANUAL §2.0.1)",
            "thinking_effort": "maximum — never lower",
            "note": "A worker configured small produces a plausible artifact that "
                    "quietly lacks the depth the gates assume. This is the most "
                    "expensive misconfiguration available.",
        },
        "orchestrator_config": {
            "context_window": "1M",
            "reads": "full gate.json, all receipts, cluster narratives, cards on demand",
            "never_reads": "graph/graph.json in full, raw corpus/text/*, cards in bulk",
        },
    }

    if not args.no_probe:
        print("probing keyless scholarly endpoints (MANUAL §2.3)...")
        for name, url in ENDPOINTS:
            caps["endpoints"][name] = probe(url)
            r = caps["endpoints"][name]
            print(f"  {name:16s} {'USABLE' if r['usable'] else 'unusable':9s} "
                  f"{r.get('status') or '-':>4} {r.get('note', r.get('format',''))}")
    else:
        caps["endpoints"] = {n: {"usable": None, "note": "not probed"}
                             for n, _ in ENDPOINTS}

    with open(os.path.join(run, "state", "capabilities.json"), "w",
              encoding="utf-8") as fh:
        json.dump(caps, fh, indent=2)

    # ---- report
    missing_crit = ([n for n, v in caps["binaries"].items()
                     if v["critical"] and not v["present"]] +
                    [n for n, v in caps["modules"].items()
                     if v["critical"] and not v["present"]])
    missing_opt = ([n for n, v in caps["binaries"].items()
                    if not v["critical"] and not v["present"]] +
                   [n for n, v in caps["modules"].items()
                    if not v["critical"] and not v["present"]])
    live = [n for n, v in caps["endpoints"].items() if v.get("usable")]

    print(f"\nscaffolded {run}")
    if placed:
        print("  templates placed: " + ", ".join(placed))
    if missing_crit:
        print(f"\n  MISSING, REQUIRED: {', '.join(missing_crit)}")
        print("  Install poppler-utils (pdftotext, pdffonts) and "
              "`pip install networkx` before starting.")
    if missing_opt:
        print(f"  missing, optional:  {', '.join(missing_opt)}")
    if live:
        print(f"\n  Scholarly endpoints USABLE: {', '.join(live)}")
        print("  Real citation traversal is available. Use it -- the manual's "
              "web-only path is a fallback, not the plan.")
    elif not args.no_probe:
        print("\n  No scholarly endpoints usable. Backward citations come from "
              "parsed reference sections; forward citations use MANUAL §2.3. "
              "Record this limitation in report §8.")

    mode = mode_doc["mode"]
    brief_what = {
        "incremental": "the FEATURE being added (not the base idea)",
        "anchored": f"the follow-up idea to the anchor ({args.anchor})",
        "retrospective": f"any context on the subject ({args.subject}) — "
                         "the paper itself is the brief",
    }.get(mode, "the idea")
    sealed_what = {
        "incremental": "prior art you know about the FEATURE",
        "anchored": "prior art you know about the FOLLOW-UP",
        "retrospective": "follow-up works you already know about",
    }.get(mode, "prior art you already know")
    print(f"""
Mode: {mode}  (state/mode.json — MANUAL §23)

Next:
  1. Write {brief_what} into {run}/00_brief.md
  2. Put {sealed_what} into {run}/SEALED_recall_check.md
     -- then do not mention it. It is the only unbiased coverage check you get.
  3. Fill in tool_mapping in {run}/state/capabilities.json
     — and confirm worker_config: 1M context, maximum thinking effort
  4. Give the agent README.md and the idea. Nothing else.""")
    if mode == "incremental":
        print("""
Incremental specifics (MANUAL §23.1):
  - P0 must produce delta + interaction components; list their IDs in
    state/mode.json delta_components.
  - Round 1 re-adjudicates inherited cards against the delta components.
  - The refresh_sweep gate fails until forward citations of base core nodes
    are swept (role: "refresh") and state/refresh_sweep.json is written.""")
    elif mode == "anchored":
        print("""
Anchored specifics (MANUAL §23.2):
  - Digest the anchor FIRST, full depth; record its card id in
    state/mode.json anchor.card_id.
  - The anchor_coverage gate fails until its forward citations are swept
    (role: "anchor_forward").""")
    elif mode == "retrospective":
        print("""
Retrospective specifics (MANUAL §23.4):
  - Digest the subject FIRST, full depth; record its card id in
    state/mode.json subject.card_id.
  - P0 extracts the subject's CLAIMS (ensemble, union-merged); list their
    IDs in state/mode.json claims. Claims are this mode's components.
  - The descent_coverage gate fails until every generation-1 citer is
    triaged in state/descent/generation_1.jsonl (role: "descent").
  - The claim_coverage gate fails until every claim has an evidence-backed
    fate in state/claim_fates.json. 'ignored' needs logged claim_check
    searches; 'overturned' needs corroboration (MANUAL §23.4).""")
    print()
    return 1 if missing_crit else 0


if __name__ == "__main__":
    sys.exit(main())
