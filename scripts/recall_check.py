#!/usr/bin/env python3
"""recall_check.py -- the P4 unsealing tool. MANUAL §0.1, §13.2.

The sealed recall check is the run's only external calibration, and until
this script existed it was graded by the entity being calibrated: the agent
read the sealed list and asserted, in prose, that it had found each item.
This script makes the grade mechanical:

  GUARD    Refuses to run before the saturation gates pass -- the seal is
           worthless if opened early, and the commonest way to open it early
           is "just to check". Override exists for the operator, loudly.

  MATCH    Parses the sealed entries and matches each against the corpus --
           cards (title, id, URL), the registry, and graph nodes -- by URL,
           arXiv id, and title-token containment. Found/missed is computed,
           not asserted.

  ANCHOR   graph_metrics.py records the sealed file's sha256 into the ledger
           every round. This script compares the current hash against the
           round-1 anchor, so a sealed file edited mid-run -- by anyone --
           is visible at P4.

Writes state/recall_check.json. Misses do not block delivery; per §13.2 they
lower every confidence in the report, and validate_report.py checks that the
report actually says so.

Usage:
    python3 scripts/recall_check.py --run-root runs/<slug>
    python3 scripts/recall_check.py --run-root runs/<slug> --operator-override

Exit codes:  0 computed (misses included) · 2 could not run · 3 refused (gates)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

TITLE_CONTAINMENT_MIN = 0.6
STOP = set("""a an the and or of for to in on with via from by is are be been
using towards toward revisiting rethinking neural deep learning based""".split())

ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
URL_RE = re.compile(r"https?://\S+")


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def tokens(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", str(text).lower())
            if w not in STOP}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_sealed(path):
    """Entries are the non-instruction lines: after the '---' separator if one
    exists, bullets or bare lines elsewhere. One entry per line."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    lines = text.splitlines()
    if "---" in [ln.strip() for ln in lines]:
        idx = [i for i, ln in enumerate(lines) if ln.strip() == "---"][0]
        lines = lines[idx + 1:]
    items = []
    for ln in lines:
        body = ln.strip().lstrip("-*+ ").strip()
        if len(body) < 6 or body.startswith("#") or body.startswith("**"):
            continue
        items.append(body)
    return items


def load_candidates(root):
    """Everything the run knows about, with ids, titles, and urls."""
    out = []
    cards_dir = os.path.join(root, "corpus", "cards")
    if os.path.isdir(cards_dir):
        for name in sorted(os.listdir(cards_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(cards_dir, name), encoding="utf-8") as fh:
                    c = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            out.append({"id": c.get("id"), "where": "card",
                        "title": (c.get("bib") or {}).get("title") or "",
                        "url": (c.get("provenance") or {}).get("url") or ""})
    for row in read_jsonl(os.path.join(root, "corpus", "index.jsonl")):
        out.append({"id": row.get("id"), "where": "index",
                    "title": row.get("title") or "",
                    "url": row.get("url") or ""})
    for gpath in (os.path.join(root, "out", "graph.json"),
                  os.path.join(root, "graph", "graph.json")):
        if os.path.exists(gpath):
            try:
                with open(gpath, encoding="utf-8") as fh:
                    graph = json.load(fh)
            except (json.JSONDecodeError, OSError):
                break
            for n in graph.get("nodes", []):
                out.append({"id": n.get("id"), "where": "graph",
                            "title": n.get("label") or n.get("title") or "",
                            "url": n.get("url") or ""})
            break
    return out


def match_item(item, candidates):
    """URL and arXiv id are decisive; otherwise title-token containment."""
    item_urls = {u.rstrip(".,)") for u in URL_RE.findall(item)}
    item_arxiv = {m[0] for m in ARXIV_RE.findall(item)}
    item_tok = tokens(URL_RE.sub(" ", item))

    best, best_score, best_how = None, 0.0, None
    for c in candidates:
        curl = c.get("url") or ""
        if item_urls and any(u in curl or curl in u for u in item_urls if curl):
            return c, 1.0, "url"
        if item_arxiv and any(a in curl or a in str(c.get("id") or "")
                              for a in item_arxiv):
            return c, 1.0, "arxiv_id"
        ctok = tokens(c.get("title") or "") | tokens(c.get("id") or "")
        if len(item_tok) >= 2 and ctok:
            score = len(item_tok & ctok) / len(item_tok)
            if score > best_score:
                best, best_score, best_how = c, score, "title_tokens"
    if best_score >= TITLE_CONTAINMENT_MIN:
        return best, round(best_score, 3), best_how
    return None, round(best_score, 3), None


def latest_gate(root):
    state = os.path.join(root, "state")
    rounds = sorted(n for n in (os.listdir(state) if os.path.isdir(state) else [])
                    if re.fullmatch(r"round_\d+", n))
    for name in reversed(rounds):
        p = os.path.join(state, name, "gate.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    return name, json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--operator-override", action="store_true",
                    help="run despite failing gates. For the OPERATOR. An "
                         "agent using this to peek early is violating §0.1.")
    ap.add_argument("--paste", default=None, metavar="FILE",
                    help="grade an operator-pasted list at P4 instead of the "
                         "sealed file (the held-back-list option)")
    ap.add_argument("--none", dest="none_provided", action="store_true",
                    help="record that the operator has no list. The run then "
                         "has NO external calibration, and the report must "
                         "say so -- this flag makes the absence auditable "
                         "instead of silent.")
    args = ap.parse_args()
    root = args.run_root

    if args.none_provided:
        doc = {
            "computed": datetime.now(timezone.utc).isoformat(),
            "source": "none_provided",
            "total": 0, "n_found": 0, "n_missed": 0, "recall": None,
            "found": [], "missed": [],
            "note": ("Operator provided no recall list. The run has no "
                     "external calibration: every statement about its own "
                     "coverage is self-graded. §8 must disclose this and "
                     "§0 confidence must not lean on coverage claims no one "
                     "outside the run checked (§13.2)."),
        }
        out = os.path.join(root, "state", "recall_check.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
        print("[recall_check] recorded: no external calibration. "
              "Disclose in §8 (validate_report checks).")
        print(f"written: {out}")
        return 0

    source = "sealed_file"
    sealed = os.path.join(root, "SEALED_recall_check.md")
    if args.paste:
        sealed, source = args.paste, "operator_pasted"
        if not os.path.exists(sealed):
            print(f"[recall_check] no file at {args.paste}")
            return 2
    elif not os.path.exists(sealed):
        print("[recall_check] no SEALED_recall_check.md -- the operator held "
              "the list back (README option). Ask them for it, save it to a "
              "file, and rerun with --paste <file>. If they have no list at "
              "all, rerun with --none so the absence is recorded, not silent.")
        return 2

    # --- guard: P4 comes after the loop, and the loop ends when gates pass.
    rname, gate = latest_gate(root)
    if gate is None:
        failing = ["<no gate.json found -- the loop has not run>"]
    else:
        failing = [k for k, v in (gate.get("gates") or {}).items()
                   if not v.get("pass") and k != "validator"]
    if failing and not args.operator_override:
        print(f"[recall_check] REFUSED. This is the P4 unsealing tool and the "
              f"gates are not passed ({rname or 'no round'}): "
              f"{', '.join(failing[:6])}")
        print("  Opening the seal early invalidates the run (§0.1). "
              "--operator-override exists for the operator only.")
        return 3
    if failing and args.operator_override:
        print(f"[recall_check] WARNING: operator override with failing gates: "
              f"{', '.join(failing[:6])}")

    # --- tamper anchor: hash now vs. hash the ledger recorded at round 1.
    # Only meaningful for the sealed file; a pasted list was never on disk.
    cur_hash, anchor, hash_consistent = None, None, None
    if source == "sealed_file":
        cur_hash = sha256_file(sealed)
        ledger = read_jsonl(os.path.join(root, "state", "ledger.jsonl"))
        anchor = next((row.get("sealed_sha256") for row in ledger
                       if row.get("sealed_sha256")), None)
        hash_consistent = (anchor == cur_hash) if anchor else None

    items = parse_sealed(sealed)
    candidates = load_candidates(root)
    found, missed = [], []
    for item in items:
        cand, score, how = match_item(item, candidates)
        if cand:
            found.append({"item": item, "matched_id": cand.get("id"),
                          "where": cand.get("where"), "how": how,
                          "score": score})
        else:
            missed.append({"item": item, "best_score": score})

    doc = {
        "computed": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "total": len(items), "n_found": len(found), "n_missed": len(missed),
        "recall": round(len(found) / len(items), 3) if items else None,
        "found": found, "missed": missed,
        "sealed_sha256": cur_hash,
        "hash_anchor": anchor,
        "hash_consistent": hash_consistent,
        "note": ("Every miss is a coverage failure: report it in §8, "
                 "investigate WHY the search missed it, and lower every "
                 "confidence in the report (§13.2). Verify found matches by "
                 "eye -- token matching can flatter."),
    }
    out = os.path.join(root, "state", "recall_check.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)

    print(f"\n=== recall check: {len(found)}/{len(items)} found "
          f"(recall {doc['recall']}) ===")
    for f in found:
        print(f"  FOUND  {f['item'][:70]}  ->  {f['matched_id']} "
              f"({f['how']}, {f['score']})")
    for m in missed:
        print(f"  MISSED {m['item'][:70]}  (best score {m['best_score']})")
    if hash_consistent is False:
        print("\n  !! SEALED FILE CHANGED SINCE ROUND 1 (hash mismatch). "
              "If the operator did not edit it, the run is invalid (§0.1). "
              "Surface this in §8 either way.")
    elif hash_consistent is None:
        print("\n  note: no round-1 hash anchor in the ledger; mid-run edits "
              "to the sealed file cannot be ruled out.")
    if missed:
        print(f"\n  {len(missed)} miss(es): each lowers every confidence in "
              "the report (§13.2), and each needs a logged explanation of why "
              "the search missed it (§22).")
    if not items:
        print("\n  note: the list is EMPTY -- the run has no external "
              "calibration. §8 must disclose this (validate_report checks).")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
