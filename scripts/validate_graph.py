#!/usr/bin/env python3
"""
validate_graph.py — the fabrication firewall. MANUAL §9.5.

Nothing in this run is trustworthy unless this exits 0. It is the one
mechanical check standing between the run and a hallucinated citation, so it
fails closed: anything it cannot verify is a defect, not a pass.

Usage:
    python3 validate_graph.py --run-root ./runs/<slug>
    python3 validate_graph.py --run-root ./runs/<slug> --json
    python3 validate_graph.py --run-root ./runs/<slug> --strict-bib

Exit codes:
    0  clean
    1  defects found (report generation is blocked)
    2  could not run (missing/malformed inputs) -- also blocking
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

VALID_NODE_TYPES = {"paper", "method", "concept", "problem", "benchmark",
                    "dataset", "claim", "group", "artifact"}
VALID_EDGE_TYPES = {"cites", "builds_on", "contradicts", "subsumes",
                    "special_case_of", "generalizes", "evaluates_on",
                    "introduces", "deprecates", "concurrent_with",
                    "competes_with", "applies_to", "reinvents"}
HIGH_CONSEQUENCE = {"subsumes", "equivalent", "special_case_of", "reinvents",
                    "contradicts"}
VALID_STATUS = {"core", "periphery", "hypothesis"}
VALID_THREAT = {"none", "low", "medium", "high", "critical"}
VALID_DEPTH = {"full_text", "abstract_only", "ocr", "partial"}
RESTRICTED_DEPTH = {"abstract_only", "partial"}   # cannot support high-consequence edges
URL_RE = re.compile(r"^https?://", re.I)
DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


class Defects:
    def __init__(self):
        self.items = defaultdict(list)

    def add(self, kind, subject, detail):
        self.items[kind].append({"subject": subject, "detail": detail})

    def __len__(self):
        return sum(len(v) for v in self.items.values())


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_cards(root, defects):
    cards_dir = os.path.join(root, "corpus", "cards")
    cards = {}
    if not os.path.isdir(cards_dir):
        defects.add("missing_dir", cards_dir, "no cards directory")
        return cards
    for name in sorted(os.listdir(cards_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cards_dir, name)
        try:
            card = read_json(path)
        except (json.JSONDecodeError, OSError) as exc:
            defects.add("card_unreadable", name, str(exc))
            continue
        cid = card.get("id")
        if not cid:
            defects.add("card_no_id", name, "card lacks an id field")
            continue
        if cid != name[:-5]:
            defects.add("card_id_mismatch", name, f"id field is {cid!r}")
        cards[cid] = card
    return cards


def validate_card(cid, card, root, defects, strict_bib):
    """A card is the evidentiary unit. Every downstream claim rests on one."""
    prov = card.get("provenance") or {}
    url = prov.get("url")
    if not url:
        defects.add("card_no_provenance_url", cid, "provenance.url missing")
    elif not URL_RE.match(str(url)):
        defects.add("card_bad_provenance_url", cid, f"not http(s): {url!r}")
    if not prov.get("retrieved"):
        defects.add("card_no_retrieved_ts", cid, "provenance.retrieved missing")

    depth = prov.get("depth")
    if depth not in VALID_DEPTH:
        defects.add("card_bad_depth", cid, f"depth={depth!r}")

    # The artifact must actually be on disk -- this is what makes provenance
    # more than a string the model can invent.
    artifact = prov.get("artifact")
    if artifact:
        if not os.path.exists(os.path.join(root, artifact)) and \
           not os.path.exists(artifact):
            defects.add("card_artifact_missing", cid, f"no file at {artifact}")
    elif depth == "full_text":
        text_dir = os.path.join(root, "corpus", "text")
        has_text = os.path.exists(os.path.join(text_dir, f"{cid}.txt")) or any(
            n.startswith(f"{cid}.chunk_") for n in
            (os.listdir(text_dir) if os.path.isdir(text_dir) else []))
        if not has_text:
            defects.add("card_no_text_on_disk", cid,
                        "depth=full_text but no extracted text found")

    # §7.2 mandatory analytic fields
    for field in ("problem", "mechanism", "delta_question"):
        if not card.get(field):
            defects.add("card_missing_field", cid, f"{field} empty or absent")
    rel = card.get("relation_to_idea") or {}
    if not rel.get("per_component"):
        defects.add("card_missing_field", cid, "relation_to_idea.per_component empty")
    if rel.get("type") and not rel.get("evidence"):
        defects.add("card_relation_no_evidence", cid,
                    f"relation type {rel.get('type')!r} without evidence anchor")
    for ev in rel.get("evidence", []) or []:
        if not ev.get("anchor"):
            defects.add("card_evidence_no_anchor", cid, "evidence entry lacks anchor")
        elif len(str(ev["anchor"]).split()) > 15:
            defects.add("card_anchor_too_long", cid,
                        f"anchor is {len(str(ev['anchor']).split())} words (max 15)")

    if card.get("threat_level") not in VALID_THREAT:
        defects.add("card_bad_threat", cid, f"threat_level={card.get('threat_level')!r}")

    bib = card.get("bib") or {}
    if not bib.get("title"):
        defects.add("card_no_title", cid, "bib.title missing")
    fpd = bib.get("first_preprint_date")
    if not fpd:
        defects.add("card_no_first_date", cid,
                    "bib.first_preprint_date missing -- priority cannot be judged")
    elif not DATE_RE.match(str(fpd)):
        defects.add("card_bad_date", cid, f"first_preprint_date={fpd!r}")

    if strict_bib and depth == "full_text":
        # Cheap anti-fabrication check: the recorded title should appear in the
        # extracted text. Catches cards written from memory rather than the PDF.
        text_path = os.path.join(root, "corpus", "text", f"{cid}.txt")
        if os.path.exists(text_path) and bib.get("title"):
            try:
                with open(text_path, encoding="utf-8", errors="ignore") as fh:
                    blob = re.sub(r"\s+", " ", fh.read(20000)).lower()
                title = re.sub(r"\s+", " ", bib["title"]).lower()
                stem = " ".join(title.split()[:6])
                if stem and stem not in blob:
                    defects.add("card_title_not_in_text", cid,
                                "recorded title not found in extracted text "
                                "-- possible fabrication or wrong PDF")
            except OSError:
                pass


def validate_graph(graph, cards, root, defects):
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    ids = set()

    for node in nodes:
        nid = node.get("id")
        if not nid:
            defects.add("node_no_id", str(node)[:60], "node lacks id")
            continue
        if nid in ids:
            defects.add("node_duplicate_id", nid, "id appears more than once")
        ids.add(nid)

        if node.get("type") not in VALID_NODE_TYPES:
            defects.add("node_bad_type", nid, f"type={node.get('type')!r}")
        if node.get("status") not in VALID_STATUS:
            defects.add("node_bad_status", nid, f"status={node.get('status')!r}")

        prov = node.get("provenance") or {}
        if not prov.get("url") or not URL_RE.match(str(prov.get("url", ""))):
            defects.add("node_no_provenance", nid,
                        "missing or malformed provenance.url -- node is unsourced")

        card_ref = node.get("card")
        if card_ref:
            if not os.path.exists(os.path.join(root, card_ref)) and \
               not os.path.exists(card_ref):
                defects.add("node_card_path_missing", nid, f"no file at {card_ref}")
        elif node.get("type") in ("paper", "artifact") and node.get("status") == "core":
            defects.add("node_no_card", nid,
                        "core paper/artifact node without a digest card")

        if node.get("date") and not DATE_RE.match(str(node["date"])):
            defects.add("node_bad_date", nid, f"date={node['date']!r}")
        if node.get("threat_level") and node["threat_level"] not in VALID_THREAT:
            defects.add("node_bad_threat", nid, f"threat={node['threat_level']!r}")

    for i, edge in enumerate(edges):
        src, tgt, etype = edge.get("source"), edge.get("target"), edge.get("type")
        label = f"{src} -{etype}-> {tgt}"
        if src not in ids:
            defects.add("edge_dangling_source", label, f"unknown node {src!r}")
        if tgt not in ids:
            defects.add("edge_dangling_target", label, f"unknown node {tgt!r}")
        if src == tgt:
            defects.add("edge_self_loop", label, "source == target")
        if etype not in VALID_EDGE_TYPES:
            defects.add("edge_bad_type", label, f"type={etype!r}")

        status = edge.get("status")
        if status not in ("verified", "hypothesis"):
            defects.add("edge_bad_status", label, f"status={status!r}")

        if etype in HIGH_CONSEQUENCE:
            if status != "verified":
                defects.add("edge_unverified_high_consequence", label,
                            f"{etype} edges must be verified before delivery")
            ev = edge.get("evidence") or []
            if not ev or not any(e.get("anchor") for e in ev):
                defects.add("edge_no_anchor", label,
                            f"{etype} edge lacks an evidence anchor")
            # §7.1: high-consequence relations require full-text on both ends
            for end in (src, tgt):
                card = cards.get(end)
                if card:
                    d = (card.get("provenance") or {}).get("depth")
                    if d in RESTRICTED_DEPTH:
                        defects.add("edge_insufficient_depth", label,
                                    f"{end} is depth={d}; {etype} requires full_text")

        if etype == "cites":
            card = cards.get(src)
            if card is not None and not card.get("references_extracted"):
                defects.add("edge_cites_without_refs", label,
                            "cites edge from a card with no parsed reference list")

    for cluster in graph.get("clusters", []) or []:
        if not cluster.get("narrative"):
            defects.add("cluster_no_narrative", cluster.get("id", "?"),
                        "cluster lacks a narrative")
        if cluster.get("expansion_state") not in ("unexplored", "partial", "saturated"):
            defects.add("cluster_bad_expansion", cluster.get("id", "?"),
                        f"expansion_state={cluster.get('expansion_state')!r}")

    return ids


def check_seal(root, defects):
    """The recall check is worthless if it leaked into the run (§0.1)."""
    sealed = os.path.join(root, "SEALED_recall_check.md")
    if not os.path.exists(sealed):
        return
    try:
        with open(sealed, encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError:
        return
    titles = [ln.strip(" -*\t") for ln in content.splitlines()
              if len(ln.strip(" -*\t")) > 25]
    if not titles:
        return
    ledger = os.path.join(root, "state", "ledger.jsonl")
    rounds = 0
    if os.path.exists(ledger):
        with open(ledger, encoding="utf-8") as fh:
            rounds = sum(1 for ln in fh if ln.strip())
    # Informational: the orchestrator asserts P4 status; we can only flag that
    # the seal exists and note it for the coverage section.
    defects.add("info_seal_present", "SEALED_recall_check.md",
                f"{len(titles)} entries; run has {rounds} logged rounds. "
                "Confirm it was first opened at P4.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict-bib", action="store_true",
                    help="cross-check recorded titles against extracted text")
    args = ap.parse_args()

    root = args.run_root
    defects = Defects()

    graph_path = os.path.join(root, "graph", "graph.json")
    if not os.path.exists(graph_path):
        print(f"[validate] FATAL: no graph at {graph_path}", file=sys.stderr)
        return 2
    try:
        graph = read_json(graph_path)
    except json.JSONDecodeError as exc:
        print(f"[validate] FATAL: malformed graph.json: {exc}", file=sys.stderr)
        return 2

    cards = load_cards(root, defects)
    for cid, card in cards.items():
        validate_card(cid, card, root, defects, args.strict_bib)
    node_ids = validate_graph(graph, cards, root, defects)
    check_seal(root, defects)

    # Orphaned cards are wasted work, not corruption -- surfaced, not fatal.
    orphans = sorted(set(cards) - node_ids)
    for cid in orphans[:50]:
        defects.add("info_card_not_in_graph", cid,
                    "digested but absent from the graph")

    blocking = {k: v for k, v in defects.items.items() if not k.startswith("info_")}
    n_blocking = sum(len(v) for v in blocking.values())

    summary = {
        "run_root": root,
        "nodes": len(graph.get("nodes", [])),
        "edges": len(graph.get("edges", [])),
        "cards": len(cards),
        "blocking_defects": n_blocking,
        "informational": len(defects) - n_blocking,
        "by_kind": {k: len(v) for k, v in sorted(defects.items.items())},
        "verdict": "PASS" if n_blocking == 0 else "FAIL",
    }

    if args.json:
        print(json.dumps({"summary": summary,
                          "defects": {k: v for k, v in defects.items.items()}},
                         indent=2, ensure_ascii=False))
    else:
        print(f"\n=== validate_graph: {summary['verdict']} ===")
        print(f"  nodes {summary['nodes']}  edges {summary['edges']}  "
              f"cards {summary['cards']}")
        print(f"  blocking defects: {n_blocking}   informational: "
              f"{summary['informational']}\n")
        for kind, items in sorted(defects.items.items()):
            marker = "i" if kind.startswith("info_") else "!"
            print(f" [{marker}] {kind}  ({len(items)})")
            for item in items[:8]:
                print(f"       {item['subject']}: {item['detail']}")
            if len(items) > 8:
                print(f"       ... and {len(items) - 8} more")
        if n_blocking:
            print("\n  Report generation is BLOCKED until these are resolved "
                  "(MANUAL §9.5, §16.5).\n")

    return 0 if n_blocking == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
