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


def have_binary(name):
    return shutil.which(name) is not None


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
    args = ap.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = os.path.join(args.root, args.slug)

    if os.path.exists(run) and not args.force:
        sys.exit(f"[init] {run} already exists. Use --force to add missing pieces, "
                 f"or pick another slug. Never scaffold over a live run.")

    for d in DIRS:
        os.makedirs(os.path.join(run, d), exist_ok=True)

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

    print(f"""
Next:
  1. Write the idea into {run}/00_brief.md
  2. Put prior art you already know into {run}/SEALED_recall_check.md
     -- then do not mention it. It is the only unbiased coverage check you get.
  3. Fill in tool_mapping in {run}/state/capabilities.json
     — and confirm worker_config: 1M context, maximum thinking effort
  4. Give the agent README.md and the idea. Nothing else.
""")
    return 1 if missing_crit else 0


if __name__ == "__main__":
    sys.exit(main())
