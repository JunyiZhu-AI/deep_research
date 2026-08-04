#!/usr/bin/env python3
"""
graph_metrics.py — structural analysis + saturation metrics for the deep
research loop. Referenced by MANUAL §9.4 and §12.

Usage:
    python3 graph_metrics.py --run-root ./runs/<slug> --round 7
    python3 graph_metrics.py --run-root ./runs/<slug> --round 7 --gates-only

Writes:
    graph/graph.json                  (nodes updated in place with centrality + cluster)
    state/round_<NN>/gate.json        (all §12 gate values, computed)
    state/ledger.jsonl                (one appended line)
    graph/snapshots/round_<NN>.json

Exit codes:
    0  metrics computed (says nothing about whether gates passed)
    2  input missing or malformed

Dependencies: networkx. Community detection uses networkx's built-in Louvain
(networkx >= 3.0); falls back to greedy modularity, then to connected
components. No other third-party requirement.
"""

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

try:
    import networkx as nx
except ImportError:
    sys.exit("networkx required:  pip install networkx")

# --- MANUAL §12 thresholds -------------------------------------------------
MIN_ROUNDS = 12
MIN_DIGESTED = 200
NEW_NODE_RATE_MAX = 0.05
NEW_NODE_RATE_CONSEC = 3
CITATION_CLOSURE_MIN = 0.90
CLOSURE_REF_FREQ = 3          # a ref counts once it appears in >=3 cards
CLUSTER_STABILITY_MIN = 0.90
CLUSTER_STABILITY_CONSEC = 2
STRATEGY_COUNT = 15
STRATEGY_EXHAUSTION_MIN = 12
COMPONENT_CARDS_MIN = 5
REDTEAM_NULL_CONSEC = 2
CARD_FIDELITY_MIN = 0.85      # §12.2 spot-audit agreement over last 3 rounds
CARD_FIDELITY_ROUNDS = 3
OPP_MIN_RECORDS = 8           # §11.5 prospector coverage
OPP_MIN_TYPES = 4
OPP_MIN_EXTENDS = 2
PROSPECTOR_START_ROUND = 8

# §23 mode gates: proof-of-work minima for the sweeps each mode requires.
REFRESH_QUERIES_MIN = 10      # distinct role:"refresh" queries (incremental)
ANCHOR_QUERIES_MIN = 10       # distinct role:"anchor_forward" queries (anchored)

HIGH_CONSEQUENCE_EDGES = {
    "subsumes", "equivalent", "special_case_of", "reinvents", "contradicts",
}


# --- io --------------------------------------------------------------------

def read_json(path, default=None):
    if not os.path.exists(path):
        if default is not None:
            return default
        sys.exit(f"[graph_metrics] missing required file: {path}")
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            sys.exit(f"[graph_metrics] malformed JSON in {path}: {exc}")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[graph_metrics] warn: bad JSONL at {path}:{n}, skipped",
                      file=sys.stderr)
    return out


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)          # atomic; orchestrator is sole writer (§2.2.1)


# --- structural analysis (§9.4) -------------------------------------------

def build_nx(graph):
    g = nx.DiGraph()
    for node in graph.get("nodes", []):
        g.add_node(node["id"], **{k: v for k, v in node.items() if k != "id"})
    for edge in graph.get("edges", []):
        if edge.get("status") == "hypothesis":
            continue                                    # unverified edges do not shape structure
        if g.has_node(edge["source"]) and g.has_node(edge["target"]):
            g.add_edge(edge["source"], edge["target"], **edge)
    return g


def centrality(g):
    if g.number_of_nodes() == 0:
        return {}
    if g.number_of_edges():
        try:
            pr = nx.pagerank(g, alpha=0.85)
        except (ImportError, ModuleNotFoundError):
            # networkx >=3 delegates pagerank to scipy. Degree centrality is a
            # coarse stand-in; the run survives, the ranking gets noisier.
            print("[graph_metrics] warn: scipy missing, pagerank approximated "
                  "by degree centrality -- pip install scipy", file=sys.stderr)
            pr = nx.degree_centrality(g)
    else:
        pr = {n: 0.0 for n in g}
    try:
        k = min(g.number_of_nodes(), 200)               # sampled for large graphs
        bt = nx.betweenness_centrality(g, k=k, seed=17)
    except Exception:
        bt = {n: 0.0 for n in g}
    return {n: {"pagerank": round(pr.get(n, 0.0), 6),
                "betweenness": round(bt.get(n, 0.0), 6)} for n in g}


def communities(g):
    """Return {node_id: cluster_label}. Undirected view; Louvain preferred."""
    if g.number_of_nodes() == 0:
        return {}
    ug = g.to_undirected()
    parts = None
    try:
        parts = nx.community.louvain_communities(ug, seed=17)
    except Exception:
        try:
            parts = list(nx.community.greedy_modularity_communities(ug))
        except Exception:
            parts = list(nx.connected_components(ug))
    return {n: f"cluster_{i:02d}" for i, part in enumerate(parts) for n in part}


def adjusted_rand_index(a, b):
    """ARI between two clusterings given as {item: label}. Pure python.

    Computed over the intersection of items so that nodes added this round do
    not by themselves register as instability.
    """
    shared = sorted(set(a) & set(b))
    n = len(shared)
    if n < 2:
        return 1.0
    table = Counter((a[i], b[i]) for i in shared)
    rows, cols = Counter(), Counter()
    for (ra, cb), cnt in table.items():
        rows[ra] += cnt
        cols[cb] += cnt

    def c2(x):
        return x * (x - 1) / 2

    index = sum(c2(v) for v in table.values())
    exp_a = sum(c2(v) for v in rows.values())
    exp_b = sum(c2(v) for v in cols.values())
    expected = exp_a * exp_b / c2(n)
    maximum = (exp_a + exp_b) / 2
    if math.isclose(maximum, expected):
        return 1.0
    return (index - expected) / (maximum - expected)


def reconcile_clusters(graph, new_membership, prev_graph):
    """Carry cluster records across rounds despite Louvain relabelling.

    Louvain assigns arbitrary indices, so cluster_03 this round may be last
    round's cluster_00. Matching records by id alone would silently discard
    every narrative and expansion_state each round -- and reset the
    cluster_expansion gate to a meaningless pass. Match by membership overlap
    instead, and give genuinely new clusters an `unexplored` stub so the gate
    blocks until they are written up and expanded.
    """
    new_members = defaultdict(set)
    for nid, cid in new_membership.items():
        new_members[cid].add(nid)

    prev_members = defaultdict(set)
    for node in (prev_graph or {}).get("nodes", []):
        if node.get("cluster"):
            prev_members[node["cluster"]].add(node["id"])
    prev_records = {c.get("id"): c for c in (graph.get("clusters") or [])}

    reconciled, claimed = [], set()
    for cid, members in sorted(new_members.items()):
        best, best_j = None, 0.0
        for pid, pmembers in prev_members.items():
            if pid in claimed or not pmembers:
                continue
            union = len(members | pmembers)
            j = len(members & pmembers) / union if union else 0.0
            if j > best_j:
                best, best_j = pid, j
        if best and best_j >= 0.5 and best in prev_records:
            rec = dict(prev_records[best])
            claimed.add(best)
            rec["id"] = cid
            rec["previous_id"] = best
            rec["drift"] = round(1 - best_j, 3)
            rec["size"] = len(members)
        else:
            rec = {"id": cid, "label": cid, "narrative": "",
                   "era": "", "status": "active",
                   "expansion_state": "unexplored", "size": len(members)}
        reconciled.append(rec)

    dissolved = [p for p in prev_records if p not in claimed]
    return reconciled, dissolved


def structural_holes(g, node_cluster, top_k=25):
    """Cluster pairs that are densely populated but thinly connected.

    Sparse regions between dense clusters usually mean missing literature, not
    absent literature (§9.4). Each returned pair becomes a frontier item.
    """
    sizes = Counter(node_cluster.values())
    between = Counter()
    for u, v in g.edges():
        cu, cv = node_cluster.get(u), node_cluster.get(v)
        if cu and cv and cu != cv:
            between[tuple(sorted((cu, cv)))] += 1
    holes = []
    labels = sorted(sizes)
    for i, ca in enumerate(labels):
        for cb in labels[i + 1:]:
            if sizes[ca] < 3 or sizes[cb] < 3:
                continue
            actual = between.get((ca, cb), 0)
            potential = sizes[ca] * sizes[cb]
            holes.append({"clusters": [ca, cb], "edges": actual,
                          "sizes": [sizes[ca], sizes[cb]],
                          "sparsity": round(1 - actual / potential, 6)})
    holes.sort(key=lambda h: (-h["sparsity"], -min(h["sizes"])))
    return holes[:top_k]


def temporal_density(graph):
    years = Counter()
    for node in graph.get("nodes", []):
        d = node.get("date") or ""
        if len(d) >= 4 and d[:4].isdigit():
            years[int(d[:4])] += 1
    if not years:
        return {"bins": {}, "suspicious_years": []}
    lo, hi = min(years), max(years)
    full = {y: years.get(y, 0) for y in range(lo, hi + 1)}
    vals = [v for v in full.values()]
    mean = sum(vals) / len(vals)
    # A year well below trend is a search-coverage failure, not a quiet year.
    suspicious = [y for y, v in full.items() if v < 0.25 * mean and y < hi]
    return {"bins": full, "mean_per_year": round(mean, 2),
            "suspicious_years": sorted(suspicious)}


# --- saturation gates (§12) ------------------------------------------------

def load_cards(run_root):
    cards_dir = os.path.join(run_root, "corpus", "cards")
    cards = []
    if not os.path.isdir(cards_dir):
        return cards
    for name in sorted(os.listdir(cards_dir)):
        if name.endswith(".json"):
            try:
                with open(os.path.join(cards_dir, name), encoding="utf-8") as fh:
                    cards.append(json.load(fh))
            except (json.JSONDecodeError, OSError):
                print(f"[graph_metrics] warn: unreadable card {name}", file=sys.stderr)
    return cards


def citation_closure(cards):
    """Of refs appearing in >=3 cards, what fraction are themselves digested?"""
    digested = {c.get("id") for c in cards if c.get("id")}
    freq = Counter()
    resolved = {}
    for card in cards:
        seen_here = set()
        for ref in card.get("references_extracted", []) or []:
            key = (ref.get("resolved_id")
                   or (ref.get("title") or "").strip().lower()[:80])
            if not key or key in seen_here:
                continue
            seen_here.add(key)
            freq[key] += 1
            if ref.get("resolved_id"):
                resolved[key] = ref["resolved_id"]
    frequent = [k for k, v in freq.items() if v >= CLOSURE_REF_FREQ]
    if not frequent:
        return 0.0, 0, []
    closed, missing = 0, []
    for key in frequent:
        rid = resolved.get(key)
        if rid and rid in digested:
            closed += 1
        else:
            missing.append({"key": key, "seen_in_cards": freq[key]})
    missing.sort(key=lambda m: -m["seen_in_cards"])
    return closed / len(frequent), len(frequent), missing[:40]


def component_coverage(cards, components):
    counts = {c: 0 for c in components}
    for card in cards:
        if card.get("provenance", {}).get("depth") != "full_text":
            continue
        for comp in (card.get("relation_to_idea", {}) or {}).get("per_component", {}):
            if comp in counts:
                counts[comp] += 1
    return counts


def strategy_exhaustion(run_root, current_round):
    """§12.1 -- exhaustion requires proof of work, not a null result.

    Under the naive definition ("last use yielded nothing") a WORSE query opens
    the gate SOONER, which inverts the metric this gate exists to enforce. A
    strategy is exhausted only if its last use issued >=8 distinct queries,
    that use was recent, and the strategy has produced something at some point
    -- or carries an explicit inapplicable flag.
    """
    queries = read_jsonl(os.path.join(run_root, "state", "seen_queries.jsonl"))
    by_strategy = defaultdict(list)
    for q in queries:
        if q.get("strategy"):
            by_strategy[q["strategy"]].append(q)

    exhausted, rejected, used = [], {}, Counter()
    for strat, qs in by_strategy.items():
        used[strat] = len(qs)
        rounds = [q.get("round", -1) for q in qs]
        last_round = max(rounds) if rounds else -1
        last_use = [q for q in qs if q.get("round", -1) == last_round]
        distinct = {(q.get("query") or "").strip().lower() for q in last_use}
        distinct.discard("")
        ever_yielded = any((q.get("new_relevant", q.get("yield", 0)) or 0) > 0
                           for q in qs)
        inapplicable = any(q.get("inapplicable") for q in qs)
        last_yield = sum((q.get("new_relevant", q.get("yield", 0)) or 0)
                         for q in last_use)

        why = []
        if last_yield != 0:
            why.append("last use still yielding")
        if len(distinct) < 8:
            why.append(f"only {len(distinct)} distinct queries on last use (need 8)")
        if last_round < current_round - 3:
            why.append(f"last used round {last_round}, stale (need within 3)")
        if not ever_yielded and not inapplicable:
            why.append("never produced a node and not flagged inapplicable")

        if why:
            rejected[strat] = "; ".join(why)
        else:
            exhausted.append(strat)
    return len(exhausted), sorted(exhausted), dict(used), rejected


def round_was_exploratory(run_root, rnd, strategy_use):
    """§12.1 -- a round only counts toward the new_node_rate streak if it
    actually searched outward. Otherwise the metric measures where the agent
    chose to look, not whether the field is exhausted."""
    queries = [q for q in read_jsonl(os.path.join(run_root, "state",
                                                  "seen_queries.jsonl"))
               if q.get("round") == rnd]
    if not queries:
        return False, "no queries logged for this round"
    ranked = sorted(strategy_use.items(), key=lambda kv: kv[1])
    least = {s for s, _ in ranked[:5]}
    used_least = {q.get("strategy") for q in queries} & least
    if len(used_least) < 2:
        return False, (f"only {len(used_least)} of the least-used strategies "
                       "exercised (need 2)")
    return True, "ok"


def redteam_round_null(run_root, rnd):
    """§12.1 -- a red-team round that found nothing AND searched nothing is
    VOID, not null. Void rounds do not advance the streak."""
    queries = [q for q in read_jsonl(os.path.join(run_root, "state",
                                                  "seen_queries.jsonl"))
               if q.get("round") == rnd and q.get("role") == "redteam"]
    distinct = {(q.get("query") or "").strip().lower() for q in queries}
    distinct.discard("")
    strategies = {q.get("strategy") for q in queries if q.get("strategy")}
    threats = [t for t in read_jsonl(os.path.join(run_root, "redteam",
                                                  "threats.jsonl"))
               if t.get("round_added") == rnd]
    found = [t for t in threats
             if t.get("threat_level") in ("medium", "high", "critical")]
    considered = [t for t in threats if t.get("verdict") == "rejected"] or \
                 [t for t in threats if t.get("threat_level") in ("none", "low")]

    if found:
        return "found", f"{len(found)} new >=medium threat(s)"
    if len(distinct) < 15 or len(strategies) < 4 or len(considered) < 3:
        return "void", (f"{len(distinct)} distinct queries across "
                        f"{len(strategies)} strategies, {len(considered)} "
                        "candidates rejected -- searched too little to count")
    return "null", "searched properly and found nothing"


def redteam_null_streak_verified(run_root, current_round):
    streak, notes, r = 0, [], current_round
    while r >= 0:
        state, why = redteam_round_null(run_root, r)
        if state == "null":
            streak += 1
            r -= 1
            continue
        notes.append(f"round {r}: {state} -- {why}")
        break
    return streak, notes


def redteam_null_streak(run_root, current_round):
    threats = read_jsonl(os.path.join(run_root, "redteam", "threats.jsonl"))
    by_round = defaultdict(list)
    for t in threats:
        if t.get("threat_level") in ("medium", "high", "critical"):
            by_round[t.get("round_added", -1)].append(t)
    streak = 0
    r = current_round
    while r >= 0 and not by_round.get(r):
        streak += 1
        r -= 1
    return streak


def card_fidelity(root, current_round, window=CARD_FIDELITY_ROUNDS):
    """§12.2 -- the hollow-corpus defense.

    Fail-closed by construction: no audits logged means no evidence the corpus
    is real, which is a failure, not a pass. This is the only check that a card
    reflects the paper it claims to summarize.
    """
    rows = read_jsonl(os.path.join(root, "state", "card_audits.jsonl"))
    recent = [r for r in rows
              if r.get("round", -1) > current_round - window]
    if not recent:
        return 0.0, 0, ["no spot-audits logged in the last "
                        f"{window} rounds"]
    scored = [r for r in recent if r.get("verdict") in ("agree", "partial", "disagree")]
    if not scored:
        return 0.0, 0, ["audit rows present but none carry a verdict"]
    weight = {"agree": 1.0, "partial": 0.5, "disagree": 0.0}
    rate = sum(weight[r["verdict"]] for r in scored) / len(scored)
    problems = []
    for r in scored:
        if r["verdict"] == "disagree":
            note = f"disagree on {r.get('card_id','?')}"
            if r.get("threat_level") in ("high", "critical"):
                note += "  -- HIGH-THREAT CARD: re-digest before continuing (§12.2)"
            problems.append(note)
    return rate, len(scored), problems


def opportunity_coverage(root, graph, current_round,
                         start_round=PROSPECTOR_START_ROUND):
    """§11.5 -- the generative half cannot stop on a null result.

    Before the prospector starts (round 8 fresh, round 4 incremental — §23.1)
    this gate is simply not yet satisfiable, which is correct: it should hold
    the run open, not wave it through.
    """
    recs = read_jsonl(os.path.join(root, "opportunities", "opportunities.jsonl"))
    complete = [o for o in recs
                if (o.get("why_now") or "").strip()
                and (o.get("falsifier") or "").strip()]
    types = {o.get("type") for o in complete if o.get("type")}
    extends = [o for o in complete if o.get("distance_from_idea") == "extends"]

    clusters = [c.get("id") for c in (graph.get("clusters") or [])]
    evaluated = {o.get("cluster") or c
                 for o in recs
                 for c in ([o.get("cluster")] if o.get("cluster") else
                           [n for n in (o.get("evidence") or [])
                            for n in n.get("clusters", [])])}
    unevaluated = [c for c in clusters if c not in evaluated]

    detail = {
        "records_complete": len(complete), "records_total": len(recs),
        "distinct_types": sorted(t for t in types if t),
        "extends": len(extends),
        "clusters_unevaluated": len(unevaluated),
        "incomplete_records": [o.get("id") for o in recs if o not in complete][:10],
    }
    ok = (current_round >= start_round
          and len(complete) >= OPP_MIN_RECORDS
          and len(types) >= OPP_MIN_TYPES
          and len(extends) >= OPP_MIN_EXTENDS
          and not unevaluated)
    return ok, detail


def load_mode(root):
    """§23.0 — state/mode.json is the single source of truth; absent = fresh."""
    doc = read_json(os.path.join(root, "state", "mode.json"), {})
    if doc.get("mode") not in ("fresh", "incremental", "anchored"):
        return {"mode": "fresh"}
    return doc


def is_delta_card(card, delta_components):
    """§23.1 — a card is delta-scoped if newly digested this run, or inherited
    but re-adjudicated against at least one delta component. An inherited card
    without delta per_component entries has not been read against the new
    question and is invisible to the delta floors on purpose."""
    if not card.get("inherited_from_base"):
        return True
    pc = (card.get("relation_to_idea") or {}).get("per_component") or {}
    return any(c in pc for c in delta_components)


def distinct_role_queries(root, role):
    queries = read_jsonl(os.path.join(root, "state", "seen_queries.jsonl"))
    distinct = {(q.get("query") or "").strip().lower()
                for q in queries if q.get("role") == role}
    distinct.discard("")
    return len(distinct)


def refresh_sweep(root):
    """§23.1 — the base corpus is frozen at delivery; the field is not.

    Fail-closed: no refresh_sweep.json, or an incomplete one, or too few
    logged refresh queries, and the gate fails. The self-reported totals are
    cross-checked against the query log so a bare 'completed: true' does not
    pass on its own."""
    doc = read_json(os.path.join(root, "state", "refresh_sweep.json"), {})
    n_queries = distinct_role_queries(root, "refresh")
    total = doc.get("base_core_nodes_total", 0) or 0
    checked = doc.get("base_core_nodes_checked", 0) or 0
    problems = []
    if not doc:
        problems.append("state/refresh_sweep.json not written")
    if not doc.get("completed"):
        problems.append("completed is not true")
    if total <= 0:
        problems.append("base_core_nodes_total missing or 0")
    if checked < total:
        problems.append(f"checked {checked} of {total} base core nodes")
    if n_queries < REFRESH_QUERIES_MIN:
        problems.append(f"{n_queries} distinct role:\"refresh\" queries "
                        f"logged (need {REFRESH_QUERIES_MIN})")
    detail = {"checked": checked, "total": total,
              "refresh_queries": n_queries,
              "new_threats_found": doc.get("new_threats_found"),
              "problems": problems}
    return not problems, detail


def anchor_coverage(root, cards_by_id, mode_doc):
    """§23.2 — the run cannot pass gates while the anchor is undigested or
    its citation neighborhood unswept."""
    anchor = mode_doc.get("anchor") or {}
    card_id = anchor.get("card_id")
    n_queries = distinct_role_queries(root, "anchor_forward")
    problems = []
    if not card_id:
        problems.append("anchor.card_id not set in state/mode.json")
    else:
        card = cards_by_id.get(card_id)
        if card is None:
            problems.append(f"no card on disk for anchor {card_id!r}")
        elif (card.get("provenance") or {}).get("depth") != "full_text":
            problems.append(f"anchor card {card_id!r} is not full_text depth")
    if n_queries < ANCHOR_QUERIES_MIN:
        problems.append(f"{n_queries} distinct role:\"anchor_forward\" queries "
                        f"logged (need {ANCHOR_QUERIES_MIN})")
    detail = {"card_id": card_id, "anchor_forward_queries": n_queries,
              "problems": problems}
    return not problems, detail


def ledger_streaks(ledger, key, predicate):
    streak = 0
    for row in reversed(ledger):
        if key in row and predicate(row[key]):
            streak += 1
        else:
            break
    return streak


# --- main ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--gates-only", action="store_true",
                    help="compute gates without rewriting graph.json")
    args = ap.parse_args()

    root = args.run_root
    rnd = args.round
    graph_path = os.path.join(root, "graph", "graph.json")
    graph = read_json(graph_path)
    ledger = read_jsonl(os.path.join(root, "state", "ledger.jsonl"))
    cards = load_cards(root)

    g = build_nx(graph)
    cent = centrality(g)
    clusters = communities(g)

    prev_graph, prev_clusters = {}, {}
    prev_snap = os.path.join(root, "graph", "snapshots", f"round_{rnd - 1:02d}.json")
    if os.path.exists(prev_snap):
        prev_graph = read_json(prev_snap, {})
        prev_clusters = {n["id"]: n.get("cluster") for n in prev_graph.get("nodes", [])
                         if n.get("cluster")}
    ari = adjusted_rand_index(prev_clusters, clusters) if prev_clusters else 0.0

    cluster_records, dissolved = reconcile_clusters(graph, clusters, prev_graph)

    # --- write structure back into the graph
    if not args.gates_only:
        for node in graph.get("nodes", []):
            nid = node["id"]
            if nid in cent:
                node["centrality"] = cent[nid]
            if nid in clusters:
                node["cluster"] = clusters[nid]
        graph["clusters"] = cluster_records
        graph.setdefault("meta", {})["round"] = rnd
        graph["meta"]["generated"] = datetime.now(timezone.utc).isoformat()
        if dissolved:
            graph["meta"]["dissolved_clusters"] = dissolved
        write_json(graph_path, graph)
        write_json(os.path.join(root, "graph", "snapshots", f"round_{rnd:02d}.json"),
                   graph)
    else:
        graph["clusters"] = cluster_records

    # --- §12 gates, thresholds scaled by run mode (§23)
    mode_doc = load_mode(root)
    mode = mode_doc["mode"]
    floors = mode_doc.get("floors") or {}
    delta_components = mode_doc.get("delta_components") or []
    if mode == "incremental":
        min_rounds_thr = floors.get("min_rounds", 6)
        min_digested_thr = floors.get("min_delta_digested", 75)
        strategy_min = floors.get("strategy_exhaustion_min", 8)
        prospector_start = floors.get("prospector_start_round", 4)
    else:
        min_rounds_thr = MIN_ROUNDS
        min_digested_thr = MIN_DIGESTED
        strategy_min = STRATEGY_EXHAUSTION_MIN
        prospector_start = PROSPECTOR_START_ROUND

    components = graph.get("meta", {}).get("components", [])
    full_text = [c for c in cards
                 if c.get("provenance", {}).get("depth") == "full_text"]
    if mode == "incremental":
        digest_pool = [c for c in full_text
                       if is_delta_card(c, delta_components)]
    else:
        digest_pool = full_text
    core_nodes = [n for n in graph.get("nodes", []) if n.get("status") == "core"]
    new_core = [n for n in core_nodes if n.get("round_added") == rnd]
    digested_this_round = [c for c in full_text if c.get("round_added") == rnd]
    new_node_rate = (len(new_core) / len(digested_this_round)
                     if digested_this_round else 1.0)

    closure, n_frequent, missing_refs = citation_closure(cards)
    comp_counts = component_coverage(cards, components)
    n_exhausted, exhausted, strategy_use, strat_rejected = strategy_exhaustion(root, rnd)
    explored, explore_why = round_was_exploratory(root, rnd, strategy_use)
    rt_streak, rt_notes = redteam_null_streak_verified(root, rnd)
    fidelity, n_audited, fidelity_problems = card_fidelity(root, rnd)
    opp_ok, opp_detail = opportunity_coverage(root, graph, rnd,
                                              start_round=prospector_start)
    unexplored = [c["id"] for c in graph.get("clusters", [])
                  if c.get("expansion_state") == "unexplored"]

    rate_streak = ledger_streaks(ledger, "new_node_rate",
                                 lambda v: v < NEW_NODE_RATE_MAX)
    # §12.1: only an exploratory round may advance the streak.
    if new_node_rate < NEW_NODE_RATE_MAX and explored:
        rate_streak += 1
    else:
        rate_streak = 0
    ari_streak = ledger_streaks(ledger, "cluster_stability",
                                lambda v: v >= CLUSTER_STABILITY_MIN)
    if ari >= CLUSTER_STABILITY_MIN:
        ari_streak += 1
    else:
        ari_streak = 0

    # Fail-closed: anything not computed is failed (§12).
    gates = {
        "min_rounds":         {"value": rnd, "threshold": min_rounds_thr,
                               "pass": rnd >= min_rounds_thr},
        "min_digested":       {"value": len(digest_pool),
                               "scope": ("delta (§23.1)" if mode == "incremental"
                                         else "all full_text"),
                               "full_text_total": len(full_text),
                               "threshold": min_digested_thr,
                               "pass": len(digest_pool) >= min_digested_thr},
        "new_node_rate":      {"value": round(new_node_rate, 4),
                               "consecutive": rate_streak,
                               "round_exploratory": explored,
                               "exploratory_note": explore_why,
                               "threshold": f"<{NEW_NODE_RATE_MAX} x{NEW_NODE_RATE_CONSEC}",
                               "pass": rate_streak >= NEW_NODE_RATE_CONSEC},
        "citation_closure":   {"value": round(closure, 4),
                               "frequent_refs": n_frequent,
                               "threshold": CITATION_CLOSURE_MIN,
                               "pass": closure >= CITATION_CLOSURE_MIN},
        "cluster_stability":  {"value": round(ari, 4), "consecutive": ari_streak,
                               "threshold": f">={CLUSTER_STABILITY_MIN} x{CLUSTER_STABILITY_CONSEC}",
                               "pass": ari_streak >= CLUSTER_STABILITY_CONSEC},
        "strategy_exhaustion": {"value": n_exhausted, "of": STRATEGY_COUNT,
                                "rejected": strat_rejected,
                                "threshold": strategy_min,
                                "pass": n_exhausted >= strategy_min},
        "component_coverage": {"value": comp_counts, "threshold": COMPONENT_CARDS_MIN,
                               "pass": bool(components) and
                                       all(v >= COMPONENT_CARDS_MIN
                                           for v in comp_counts.values())},
        "cluster_expansion":  {"value": len(unexplored), "unexplored": unexplored,
                               "threshold": 0, "pass": len(unexplored) == 0},
        "redteam_null":       {"value": rt_streak, "why_streak_broke": rt_notes,
                               "threshold": REDTEAM_NULL_CONSEC,
                               "pass": rt_streak >= REDTEAM_NULL_CONSEC},
        "card_fidelity":      {"value": round(fidelity, 4), "audited": n_audited,
                               "problems": fidelity_problems[:6],
                               "threshold": CARD_FIDELITY_MIN,
                               "pass": (n_audited > 0 and fidelity >= CARD_FIDELITY_MIN)},
        "opportunity_coverage": {"value": opp_detail, "threshold":
                                 f">={OPP_MIN_RECORDS} records, >={OPP_MIN_TYPES} "
                                 f"types, >={OPP_MIN_EXTENDS} extends, 0 unevaluated",
                                 "pass": opp_ok},
        "validator":          {"value": "run validate_graph.py separately",
                               "threshold": 0, "pass": False},
    }
    # §23 mode gates — present only in their mode, fail-closed like the rest.
    if mode == "incremental":
        rs_ok, rs_detail = refresh_sweep(root)
        gates["refresh_sweep"] = {"value": rs_detail,
                                  "threshold": "completed sweep + "
                                               f">={REFRESH_QUERIES_MIN} refresh queries",
                                  "pass": rs_ok}
        if not delta_components:
            # No declared delta components means the delta floors measure
            # nothing. Fail min_digested explicitly rather than silently.
            gates["min_digested"]["pass"] = False
            gates["min_digested"]["scope"] = ("delta (§23.1) -- FAILING: "
                                              "mode.json delta_components is "
                                              "empty; fill it after P0")
    elif mode == "anchored":
        cards_by_id = {c.get("id"): c for c in cards if c.get("id")}
        ac_ok, ac_detail = anchor_coverage(root, cards_by_id, mode_doc)
        gates["anchor_coverage"] = {"value": ac_detail,
                                    "threshold": "anchor digested full_text + "
                                                 f">={ANCHOR_QUERIES_MIN} forward queries",
                                    "pass": ac_ok}
    all_pass = all(gate["pass"] for gate in gates.values())

    # --- guidance: the failing gate drives next round's assignment mix (§12)
    guidance = []
    if not gates["citation_closure"]["pass"]:
        guidance.append({"gate": "citation_closure", "action": "digest_frequent_refs",
                         "targets": missing_refs[:10]})
    if not gates["cluster_expansion"]["pass"]:
        guidance.append({"gate": "cluster_expansion", "action": "expand_clusters",
                         "targets": unexplored})
    if not gates["component_coverage"]["pass"]:
        thin = [c for c, v in comp_counts.items() if v < COMPONENT_CARDS_MIN]
        guidance.append({"gate": "component_coverage", "action": "redteam_component",
                         "targets": thin})
    if not gates["strategy_exhaustion"]["pass"]:
        unused = [f"S{i}" for i in range(1, STRATEGY_COUNT + 1)
                  if strategy_use.get(f"S{i}", 0) == 0]
        least = sorted(strategy_use.items(), key=lambda kv: kv[1])[:3]
        guidance.append({"gate": "strategy_exhaustion", "action": "exercise_strategies",
                         "targets": unused or [s for s, _ in least],
                         "why_rejected": strat_rejected})
    if not gates["redteam_null"]["pass"]:
        guidance.append({"gate": "redteam_null", "action": "sustain_redteam",
                         "targets": []})
    if not gates["card_fidelity"]["pass"]:
        guidance.append({"gate": "card_fidelity", "action": "spot_audit_cards",
                         "targets": fidelity_problems[:5] or
                                    ["log 3 audits to state/card_audits.jsonl"]})
    if not gates["opportunity_coverage"]["pass"] and rnd >= prospector_start:
        guidance.append({"gate": "opportunity_coverage", "action": "run_prospector",
                         "targets": (opp_detail["incomplete_records"] or
                                     [f"{opp_detail['clusters_unevaluated']} clusters "
                                      "not yet evaluated for opportunity"])})
    if "refresh_sweep" in gates and not gates["refresh_sweep"]["pass"]:
        guidance.append({"gate": "refresh_sweep", "action": "run_refresh_sweep",
                         "targets": gates["refresh_sweep"]["value"]["problems"][:4]})
    if "anchor_coverage" in gates and not gates["anchor_coverage"]["pass"]:
        guidance.append({"gate": "anchor_coverage",
                         "action": "sweep_anchor_forward_citations",
                         "targets": gates["anchor_coverage"]["value"]["problems"][:4]})

    undigested_central = sorted(
        ({"id": n["id"], "pagerank": cent.get(n["id"], {}).get("pagerank", 0)}
         for n in graph.get("nodes", [])
         if not os.path.exists(os.path.join(root, "corpus", "cards", f"{n['id']}.json"))),
        key=lambda x: -x["pagerank"])[:20]

    gate_doc = {
        "round": rnd,
        "mode": mode,
        "computed": datetime.now(timezone.utc).isoformat(),
        "gates": gates,
        "all_pass": all_pass,
        "report_unlocked": False,          # requires validator exit 0 as well
        "next_round_guidance": guidance,
        "frontier_hints": {
            "undigested_high_centrality": undigested_central,
            "structural_holes": structural_holes(g, clusters),
            "temporal_density": temporal_density(graph),
            "missing_frequent_refs": missing_refs[:20],
        },
    }
    rounddir = os.path.join(root, "state", f"round_{rnd:02d}")

    # gate.json is the FULL document and is what the orchestrator reads. On a
    # 1M-context orchestrator the whole thing costs ~3.6k tokens and every
    # field in it changes a decision -- the per-strategy rejection reasons say
    # exactly which strategy to run next, and summarising them away would trade
    # decision quality for a resource that is not scarce.
    #
    # gate_summary.json is a trimmed fallback for small-context harnesses.
    # Prefer gate.json wherever the window allows it.
    write_json(os.path.join(rounddir, "gate.json"), gate_doc)
    def trim(seq, n=5):
        return (seq or [])[:n]

    compact = {
        "round": rnd,
        "all_pass": all_pass,
        "failing": [k for k, v in gates.items() if not v["pass"]],
        "counts": {"nodes_core": len(core_nodes), "cards_full_text": len(full_text),
                   "clusters": len(set(clusters.values()))},
        "metrics": {k: gates[k].get("value") for k in
                    ("new_node_rate", "citation_closure", "cluster_stability",
                     "strategy_exhaustion", "redteam_null", "card_fidelity")},
        "do_next": [{"action": g["action"],
                     "targets": [str(t if not isinstance(t, dict)
                                     else t.get("key") or t.get("id") or t)[:60]
                                 for t in trim(g.get("targets"))]}
                    for g in guidance],
        "top_frontier": trim([u["id"] for u in undigested_central]),
        "holes": trim([h["clusters"] for h in
                       gate_doc["frontier_hints"]["structural_holes"]], 3),
        "full": f"state/round_{rnd:02d}/gate.json",
        "note": "Trimmed fallback. Read gate.json instead unless context-bound.",
    }
    write_json(os.path.join(rounddir, "gate_summary.json"), compact)

    ledger_line = {
        "round": rnd,
        "mode": mode,
        "ts": datetime.now(timezone.utc).isoformat(),
        "nodes_total": len(graph.get("nodes", [])),
        "nodes_core": len(core_nodes),
        "edges_total": len(graph.get("edges", [])),
        "edges_hypothesis": sum(1 for e in graph.get("edges", [])
                                if e.get("status") == "hypothesis"),
        "cards_full_text": len(full_text),
        "cards_delta_full_text": (len(digest_pool) if mode == "incremental"
                                  else None),
        "cards_total": len(cards),
        "new_node_rate": round(new_node_rate, 4),
        "citation_closure": round(closure, 4),
        "cluster_stability": round(ari, 4),
        "strategies_exhausted": n_exhausted,
        "redteam_null_streak": rt_streak,
        "card_fidelity": round(fidelity, 4),
        "opportunities_complete": opp_detail["records_complete"],
        "clusters": len(set(clusters.values())),
        "all_gates_pass": all_pass,
    }
    ledger_path = os.path.join(root, "state", "ledger.jsonl")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ledger_line, ensure_ascii=False) + "\n")

    print(json.dumps({"round": rnd, "all_gates_pass": all_pass,
                      "failing": [k for k, v in gates.items() if not v["pass"]],
                      "ledger": ledger_line}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
