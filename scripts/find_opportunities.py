#!/usr/bin/env python3
"""
find_opportunities.py -- computes typed opportunity CANDIDATES from graph
structure. MANUAL §11.3.

This does not find opportunities. It finds the structural signatures where
opportunities live, so the prospector spends its slots reading and falsifying
rather than guessing where to look. Every candidate it emits still needs a
`why_now` and a searched falsifier from a prospector worker before it becomes
a record in opportunities/opportunities.jsonl.

Usage:
    python3 find_opportunities.py --run-root ./runs/<slug>
    python3 find_opportunities.py --run-root ./runs/<slug> --min-cluster 4

Writes:
    opportunities/candidates.json        typed candidates with evidence
    opportunities/future_work_clusters.json
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

try:
    import networkx as nx
except ImportError:
    sys.exit("networkx required:  pip install networkx")

STOP = set("""a an the and or of for to in on with without by from as at is are was
were be been being this that these those we our it its their they can could may
might will would should must not no than then thus hence such very more most
some any each other another using used use based approach method model models
results result show shows shown propose proposed work works paper study also
however while when where which who whom how what why into over under between
across during before after above below same different new novel""".split())


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def load_cards(root):
    out = {}
    d = os.path.join(root, "corpus", "cards")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            c = read_json(os.path.join(d, name))
            if c and c.get("id"):
                out[c["id"]] = c
    return out


def year(d):
    try:
        return int(str(d)[:4])
    except (ValueError, TypeError):
        return None


def tokens(text):
    return {w for w in re.findall(r"[a-z][a-z\-]{3,}", str(text).lower())
            if w not in STOP}


# How much a card's claims can bear (MANUAL §11.3). Weighting orders the
# prospector's attention; it never removes a candidate.
STRENGTH_W = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}
EVIDENCE_W = {"benchmark": 1.0, "ablation": 1.0, "theory": 0.8, "anecdote": 0.4}


def evidence_weight(card):
    """Solidity of a card's claims, in (0, 1].

    The strongest claim sets the level: a paper with one strong benchmark
    result and three anecdotes is a solid source. A card with no scored
    claims gets 0.5 -- unknown, not bad.
    """
    best = 0.0
    for cl in (card or {}).get("claims", []) or []:
        w = STRENGTH_W.get(cl.get("strength"), 0.3) * \
            EVIDENCE_W.get(cl.get("evidence_type"), 0.6)
        best = max(best, w)
    return round(best, 3) if best > 0 else 0.5


# --- detectors -------------------------------------------------------------

def transfer_gaps(graph, cards, node_cluster, min_cluster, top_k=12):
    """Two threads working the same problem shape with no methodological contact.

    The most reliable paper generator in the literature: thread A solved a
    problem with technique T; thread B has the same problem and never heard of T.
    """
    members = defaultdict(list)
    for nid, cid in node_cluster.items():
        members[cid].append(nid)

    prob_tokens, contact = {}, Counter()
    for cid, ids in members.items():
        if len(ids) < min_cluster:
            continue
        bag = Counter()
        for nid in ids:
            bag.update(tokens((cards.get(nid) or {}).get("problem", "")))
        prob_tokens[cid] = {w for w, n in bag.items() if n >= max(2, len(ids) // 6)}

    for e in graph.get("edges", []):
        ca, cb = node_cluster.get(e.get("source")), node_cluster.get(e.get("target"))
        if ca and cb and ca != cb:
            contact[tuple(sorted((ca, cb)))] += 1

    out = []
    keys = sorted(prob_tokens)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            shared = prob_tokens[a] & prob_tokens[b]
            if len(shared) < 3:
                continue
            edges = contact.get(tuple(sorted((a, b))), 0)
            density = edges / (len(members[a]) * len(members[b]))
            if density > 0.004:
                continue                      # already in contact; not a gap
            out.append({
                "type": "transfer_gap",
                "clusters": [a, b],
                "shared_problem_terms": sorted(shared)[:10],
                "cross_edges": edges,
                "sizes": [len(members[a]), len(members[b])],
                "score": round(len(shared) * (1 - density * 200), 3),
                "prospector_task": (f"Threads {a} and {b} both work on "
                                    f"{', '.join(sorted(shared)[:4])} but barely cite each "
                                    f"other. Identify the technique one has that the other "
                                    f"lacks, then falsify: has anyone already carried it over?"),
            })
    out.sort(key=lambda o: -o["score"])
    return out[:top_k]


def expired_blockers(cards, top_k=15):
    """Obstacles authors named that a later card reports as removed."""
    out = []
    for cid, card in cards.items():
        for blocker in card.get("blocked_by", []) or []:
            if blocker.get("still_holds") is False:
                out.append({
                    "type": "expired_blocker",
                    "node": cid,
                    "obstacle": blocker.get("what", ""),
                    "removed_by": blocker.get("note", ""),
                    "date": (card.get("bib") or {}).get("first_preprint_date", ""),
                    "score": 3.0,
                    "prospector_task": ("This obstacle is recorded as no longer holding. "
                                        "Falsify: did anyone revisit the blocked work "
                                        "after it lifted?"),
                })
    # abandoned-thread signature: a cluster whose newest node is years stale
    return out[:top_k]


def stale_threads(graph, node_cluster, cards, now_year, gap=4, top_k=10):
    latest = defaultdict(lambda: 0)
    size = Counter()
    for n in graph.get("nodes", []):
        cid = node_cluster.get(n["id"])
        if not cid:
            continue
        size[cid] += 1
        y = year(n.get("date"))
        if y:
            latest[cid] = max(latest[cid], y)
    out = []
    for cid, last in latest.items():
        if size[cid] >= 4 and (now_year - last) >= gap:
            out.append({
                "type": "expired_blocker",
                "subtype": "stale_thread",
                "cluster": cid, "last_activity": last, "size": size[cid],
                "score": round((now_year - last) * 0.5 + size[cid] * 0.1, 3),
                "prospector_task": (f"Thread {cid} has been silent since {last}. "
                                    "Find the stated reason it stopped, then determine "
                                    "whether that reason still holds today. If it does "
                                    "not, that is an expired_blocker; if it was simply "
                                    "solved, it is not an opportunity."),
            })
    out.sort(key=lambda o: -o["score"])
    return out[:top_k]


def unresolved_disputes(graph, cards, top_k=12):
    """contradicts edges that no later node cites from both sides.

    A dispute is only as real as its worst evidence: two strong-benchmark
    papers contradicting each other is an experiment waiting to happen; two
    anecdotes contradicting each other is noise. Weight by the weaker side.
    """
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    cited_by = defaultdict(set)
    for e in graph.get("edges", []):
        if e.get("type") == "cites":
            cited_by[e["target"]].add(e["source"])
    out = []
    for e in graph.get("edges", []):
        if e.get("type") != "contradicts":
            continue
        a, b = e.get("source"), e.get("target")
        both = cited_by.get(a, set()) & cited_by.get(b, set())
        ya, yb = year((nodes.get(a) or {}).get("date")), year((nodes.get(b) or {}).get("date"))
        later = {n for n in both
                 if (year((nodes.get(n) or {}).get("date")) or 0) > max(ya or 0, yb or 0)}
        if not later:
            wa, wb = evidence_weight(cards.get(a)), evidence_weight(cards.get(b))
            out.append({
                "type": "unresolved_dispute",
                "nodes": [a, b], "dates": [ya, yb], "settling_papers": 0,
                "evidence_weights": [wa, wb],
                "score": round(2.0 + 4.0 * min(wa, wb), 3),
                "prospector_task": ("These two contradict each other and nothing later "
                                    "cites both. Falsify: has anyone resolved this "
                                    "empirically? If not, the resolving experiment is "
                                    "the opportunity."),
            })
    out.sort(key=lambda o: -o["score"])
    return out[:top_k]


def orphaned_artifacts(graph, cards, now_year, top_k=12):
    """Usable artifacts old enough to have follow-up work, that have none."""
    indeg = Counter()
    for e in graph.get("edges", []):
        indeg[e.get("target")] += 1
    out = []
    for n in graph.get("nodes", []):
        y = year(n.get("date"))
        if not y or (now_year - y) < 2:
            continue
        if n.get("type") not in ("artifact", "dataset", "benchmark"):
            continue
        if indeg[n["id"]] <= 1:
            out.append({
                "type": "orphaned_artifact",
                "node": n["id"], "kind": n.get("type"), "date": n.get("date"),
                "in_degree": indeg[n["id"]],
                "score": round((now_year - y) * 0.4 + 2, 3),
                "prospector_task": ("This artifact exists and nothing builds on it. "
                                    "Falsify: is it unused because it is bad, or "
                                    "because nobody noticed? Only the second is an "
                                    "opportunity."),
            })
    out.sort(key=lambda o: -o["score"])
    return out[:top_k]


def scaling_frontier(graph, cards, now_year, top_k=10):
    """Old, central results never retested since the regime changed."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    cited_by = defaultdict(list)
    for e in graph.get("edges", []):
        if e.get("type") in ("cites", "builds_on"):
            cited_by[e["target"]].append(e["source"])
    out = []
    for nid, n in nodes.items():
        y = year(n.get("date"))
        pr = (n.get("centrality") or {}).get("pagerank", 0)
        if not y or (now_year - y) < 6 or pr < 0.002:
            continue
        recent = [c for c in cited_by.get(nid, [])
                  if (year((nodes.get(c) or {}).get("date")) or 0) >= now_year - 2]
        if not recent:
            out.append({
                "type": "scaling_frontier",
                "node": nid, "date": n.get("date"), "pagerank": round(pr, 5),
                "citations_last_2y": 0,
                "score": round(pr * 400 + (now_year - y) * 0.2, 3),
                "prospector_task": ("A load-bearing result from a different regime "
                                    "with no recent follow-up. Falsify: has it been "
                                    "retested at current scale? If not, does the "
                                    "original argument survive the regime change?"),
            })
    out.sort(key=lambda o: -o["score"])
    return out[:top_k]


def evaluation_gaps(cards, top_k=10, min_points=4):
    """Benchmarks whose reported numbers stopped moving.

    Keyed on (benchmark, metric, split) -- NOT on benchmark name. Papers report
    accuracy, F1, and EM on the same benchmark under different splits; pooling
    them produces a confident and wrong saturation claim. A key with too few
    comparable points yields no candidate rather than a weak one: "insufficient
    comparable data" is a real finding, an invented trend is not.
    """
    series = defaultdict(list)
    for cid, card in cards.items():
        y = year((card.get("bib") or {}).get("first_preprint_date"))
        for r in card.get("results", []) or []:
            b, v, m = r.get("benchmark"), r.get("value"), r.get("metric")
            split = r.get("split") or r.get("subset") or "unspecified"
            if b and m and isinstance(v, (int, float)) and y:
                series[(b, m, split)].append((y, v, cid))
    out = []
    for key, pts in series.items():
        bench, metric, split = key
        if len(pts) < max(6, min_points):
            continue
        pts.sort()
        mid = len(pts) // 2
        early = sum(v for _, v, _ in pts[:mid]) / mid
        late = sum(v for _, v, _ in pts[mid:]) / (len(pts) - mid)
        gain = late - early
        if abs(gain) < 0.02 * max(abs(early), 1e-9):
            out.append({
                "type": "evaluation_gap",
                "benchmark": bench, "metric": metric, "split": split,
                "n_points": len(pts), "span": [pts[0][0], pts[-1][0]],
                "improvement": round(gain, 5),
                "comparability": "matched (benchmark, metric, split)",
                "score": round(len(pts) * 0.3 + 2, 3),
                "prospector_task": (f"{bench} ({metric}, {split}) has stopped moving "
                                    "across comparable reports. Falsify: has a "
                                    "successor benchmark been proposed and adopted? "
                                    "If not, the successor is the opportunity. Confirm "
                                    "the protocols really are comparable before "
                                    "recording this."),
            })
    out.sort(key=lambda o: -o["score"])
    return out[:top_k]


def cluster_future_work(cards, node_cluster, min_support=3):
    """The highest-yield signal: limitations many groups named independently.

    One paper's future-work section is boilerplate. The same gap named by
    eleven groups across four years and still unaddressed is the strongest
    opportunity evidence this run can produce -- and it only exists because
    every card carried its share.

    Support is weighted by each distinct card's claim solidity (§11.3), so
    eight groups blocked despite benchmark-grade evidence outrank eight
    anecdotal mentions. The min_support gate stays on the raw count --
    weighting reorders themes, it never suppresses one.
    """
    entries = []
    for cid, card in cards.items():
        y = year((card.get("bib") or {}).get("first_preprint_date"))
        for text in (card.get("future_work_stated") or []):
            entries.append({"node": cid, "year": y, "text": text,
                            "tok": tokens(text), "cluster": node_cluster.get(cid)})
        for a in (card.get("unexamined_assumption") or []):
            entries.append({"node": cid, "year": y, "text": a,
                            "tok": tokens(a), "cluster": node_cluster.get(cid),
                            "kind": "assumption"})

    used, groups = set(), []
    for i, e in enumerate(entries):
        if i in used or len(e["tok"]) < 2:
            continue
        bucket = [e]
        used.add(i)
        for j in range(i + 1, len(entries)):
            if j in used:
                continue
            o = entries[j]
            union = len(e["tok"] | o["tok"])
            if union and len(e["tok"] & o["tok"]) / union >= 0.34:
                bucket.append(o)
                used.add(j)
        if len(bucket) >= min_support:
            years = [b["year"] for b in bucket if b["year"]]
            shared = set.intersection(*[b["tok"] for b in bucket]) or e["tok"]
            nodes = {b["node"] for b in bucket}
            weighted = sum(evidence_weight(cards.get(n)) for n in nodes)
            groups.append({
                "theme": " / ".join(sorted(shared)[:6]),
                "support": len(bucket),
                "distinct_nodes": len(nodes),
                "weighted_support": round(weighted, 3),
                "clusters": sorted({b["cluster"] for b in bucket if b["cluster"]}),
                "year_span": [min(years), max(years)] if years else None,
                "kind": "assumption" if any(b.get("kind") == "assumption" for b in bucket)
                        else "future_work",
                "examples": [{"node": b["node"], "text": b["text"][:220]}
                             for b in bucket[:6]],
                "score": round(weighted * 1.2 +
                               ((max(years) - min(years)) * 0.4 if len(years) > 1 else 0), 3),
            })
    groups.sort(key=lambda g: -g["score"])
    return groups


# --- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--min-cluster", type=int, default=4)
    ap.add_argument("--now-year", type=int, default=datetime.now(timezone.utc).year)
    args = ap.parse_args()

    root = args.run_root
    graph = read_json(os.path.join(root, "out", "graph.json")) or \
            read_json(os.path.join(root, "graph", "graph.json"))
    if not graph:
        sys.exit(f"[opportunities] no graph under {root}")
    cards = load_cards(root)
    node_cluster = {n["id"]: n.get("cluster") for n in graph.get("nodes", [])
                    if n.get("cluster")}
    ny = args.now_year

    candidates = (
        transfer_gaps(graph, cards, node_cluster, args.min_cluster) +
        expired_blockers(cards) +
        stale_threads(graph, node_cluster, cards, ny) +
        unresolved_disputes(graph, cards) +
        orphaned_artifacts(graph, cards, ny) +
        scaling_frontier(graph, cards, ny) +
        evaluation_gaps(cards)
    )
    for i, c in enumerate(candidates, 1):
        c["candidate_id"] = f"CAND-{i:03d}"
        c["status"] = "needs_why_now_and_falsifier"
    candidates.sort(key=lambda c: -c.get("score", 0))

    fw = cluster_future_work(cards, node_cluster)

    outdir = os.path.join(root, "opportunities")
    os.makedirs(outdir, exist_ok=True)
    doc = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "note": ("Structural candidates only. Each still requires a why_now and a "
                 "SEARCHED falsifier from a prospector worker before it becomes an "
                 "opportunity record (MANUAL §11.4)."),
        "type_counts": dict(Counter(c["type"] for c in candidates)),
        "candidates": candidates,
    }
    with open(os.path.join(outdir, "candidates.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(outdir, "future_work_clusters.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": doc["generated"], "clusters": fw}, fh,
                  indent=2, ensure_ascii=False)

    print(json.dumps({
        "candidates": len(candidates),
        "by_type": doc["type_counts"],
        "future_work_themes": len(fw),
        "top_themes": [{"theme": g["theme"], "support": g["support"],
                        "span": g["year_span"]} for g in fw[:5]],
        "types_available": len(doc["type_counts"]),
        "gate_hint": ("§11.5 needs >=4 distinct types with why_now + falsifier; "
                      f"{len(doc['type_counts'])} types have structural candidates"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
