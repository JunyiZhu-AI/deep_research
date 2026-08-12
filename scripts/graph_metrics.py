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
import hashlib
import json
import math
import os
import re
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

# §23.4 retrospective mode.
DESCENT_QUERIES_MIN = 15      # distinct role:"descent" queries
DESCENT_TRIAGE_MIN = 0.95     # fraction of generation-1 rows triaged
CLAIM_CHECK_QUERIES_MIN = 5   # per-claim proof of search for an `ignored` fate
OVERTURN_RELIABILITY_MIN = 0.75
CLAIM_FATES = {"held", "disputed", "overturned", "developed", "varied",
               "repurposed", "abandoned", "ignored"}

# §23.4 reliability inputs (weighting, never censorship — banned behavior 46).
STRENGTH_W = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}
EVIDENCE_W = {"benchmark": 1.0, "ablation": 1.0, "theory": 0.8, "anecdote": 0.4}

# §23.5 concept mode.
SENSE_CONFIRM_MIN = 2         # retrieved uses per sense
ALIAS_QUERIES_MIN = 2         # logged queries containing each alias
SIBLING_CARDS_MIN = 3         # digested cards per sibling concept
SIBLINGS_MIN = 2
PROPERTY_CHECK_QUERIES_MIN = 5
PROPERTY_STATUSES = {"replicated", "demonstrated", "contested", "refuted",
                     "folklore"}

# §23.6 problem mode.
SOLUTION_CHECK_QUERIES_MIN = 5
SOLUTION_STATUSES = {"proven", "promising", "contested", "failed",
                     "unvalidated"}
PROVEN_INDEPENDENT_GROUPS_MIN = 2

# The run-state contract (modes, shape validation, matching) lives in
# state_io.py and is copied into every run beside this file. No literal
# fallback copies: duplicated definitions are what drifted before.
try:
    from state_io import (VALID_MODES, load_mode, read_json_object,
                          read_jsonl_objects, cap, as_dict, as_id_list,
                          as_count, as_evidence, as_node_refs,
                          id_present, term_pattern)
except ImportError:
    sys.exit("[graph_metrics] state_io.py must sit beside this script "
             "(init_run.py copies it into the run).")

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
    """Object rows only. Non-object rows are reported once per run by
    `jsonl_problems()` rather than handed to code that will call .get()."""
    rows, problems = read_jsonl_objects(path)
    if problems:
        _JSONL_PROBLEMS.extend(problems)
    return rows


_JSONL_PROBLEMS = []


def jsonl_problems():
    """Malformed-row complaints accumulated by any read_jsonl call, deduped
    so a log read five times is reported once."""
    seen, out = set(), []
    for p in _JSONL_PROBLEMS:
        if p not in seen:
            seen.add(p)
            out.append(p)
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
                         start_round=PROSPECTOR_START_ROUND,
                         cards_by_id=None):
    """§11.5 -- the generative half cannot stop on a null result.

    Before the prospector starts (round 8 fresh, round 4 incremental — §23.1)
    this gate is simply not yet satisfiable, which is correct: it should hold
    the run open, not wave it through. Evidence nodes cited by opportunity
    records must exist in the corpus — a fabricated citation here flows
    straight into report §7 (§1.4).
    """
    recs = read_jsonl(os.path.join(root, "opportunities", "opportunities.jsonl"))
    # One pass over evidence: shape-check it, collect cited nodes and the
    # clusters each record evaluated. Every consumer below reads these,
    # so no later comprehension re-walks raw agent JSON.
    shape, fabricated, evaluated = [], [], set()
    for o in recs:
        oid = o.get("id", "?")
        own = f"opportunities.jsonl[{oid}]"
        if o.get("cluster"):
            evaluated.add(o.get("cluster"))
        for e in as_evidence(o.get("evidence"), own, shape):
            for n in as_node_refs(e.get("nodes"), f"{own} evidence.nodes",
                                  shape):
                if cards_by_id is not None and n not in cards_by_id:
                    fabricated.append({"opportunity": oid, "node": n})
            for c in as_node_refs(e.get("clusters"),
                                  f"{own} evidence.clusters", shape):
                evaluated.add(c)

    complete = [o for o in recs
                if (o.get("why_now") or "").strip()
                and (o.get("falsifier") or "").strip()]
    types = {o.get("type") for o in complete if o.get("type")}
    extends = [o for o in complete if o.get("distance_from_idea") == "extends"]

    clusters = [c.get("id") for c in (graph.get("clusters") or [])]
    unevaluated = [c for c in clusters if c not in evaluated]

    detail = {
        "records_complete": len(complete), "records_total": len(recs),
        "distinct_types": sorted(t for t in types if t),
        "extends": len(extends),
        "clusters_unevaluated": len(unevaluated),
        "incomplete_records": [o.get("id") for o in recs if o not in complete][:10],
        "fabricated_evidence_nodes": fabricated[:10],
        "malformed_records": cap(shape, 4, "malformed opportunity records"),
    }
    ok = (current_round >= start_round
          and len(complete) >= OPP_MIN_RECORDS
          and len(types) >= OPP_MIN_TYPES
          and len(extends) >= OPP_MIN_EXTENDS
          and not unevaluated
          and not fabricated
          and not shape)
    return ok, detail


def card_authors(card):
    """Normalized author set of a card. Empty means UNKNOWN, not 'nobody' —
    callers must treat an empty set as inability to establish independence,
    never as universal independence."""
    return {str(a).strip().lower()
            for a in ((card or {}).get("bib") or {}).get("authors", []) or []}


def max_disjoint_groups(groups):
    """Size of the largest set of pairwise-disjoint author groups.

    Deterministic and never order-dependent: groups are deduplicated and
    sorted canonically, a size-ascending greedy pass sets a floor, and an
    exact branch-and-bound — initialized at that floor, so it can only
    improve — computes the answer. Used by §23.5 `replicated` and §23.6
    `proven` so the two gates cannot disagree on the same evidence.

    The exact search is cheap in practice (24 adversarial groups measured
    at 2-6 ms) because the bound prunes hard, but a pathological input
    should slow a gate, never hang a run: past a generous node budget the
    search stops and returns the best found so far, which is a floor and
    therefore still safe to compare a self-reported count against."""
    uniq = sorted({frozenset(g) for g in groups if g},
                  key=lambda s: (len(s), tuple(sorted(s))))
    taken, best = frozenset(), 0
    for g in uniq:
        if not (g & taken):
            taken |= g
            best += 1
    budget = [200000]

    def rec(i, chosen, count):
        nonlocal best
        best = max(best, count)
        budget[0] -= 1
        if budget[0] <= 0 or i >= len(uniq) or \
                count + (len(uniq) - i) <= best:
            return
        if not (uniq[i] & chosen):
            rec(i + 1, chosen | uniq[i], count + 1)
        rec(i + 1, chosen, count)

    rec(0, frozenset(), 0)
    return best


def role_target_checks(root, role):
    """Proof-of-search accounting for the *_check roles (§12.0, §23.4–§23.6).

    Returns (per_tag, untagged, problems): distinct query texts per
    normalized tag, texts carrying no readable tag, and legible complaints
    for rows the accounting cannot read.
    """
    per_tag, untagged, problems = defaultdict(set), set(), []
    for q in read_jsonl(os.path.join(root, "state", "seen_queries.jsonl")):
        if q.get("role") != role:
            continue
        text = str(q.get("query") or "").strip().lower()
        if not text:
            continue
        claim = q.get("claim")
        if claim is None:
            untagged.add(text)
            continue
        tags = [claim] if isinstance(claim, str) else \
               list(claim) if isinstance(claim, (list, tuple)) else None
        if tags is None or not all(isinstance(t, str) for t in tags):
            problems.append(f"{role} row has unreadable claim {claim!r} — "
                            "use a string or list of strings (§12.0)")
            untagged.add(text)
            continue
        clean = [t.strip().lower() for t in tags if t.strip()]
        if not clean:
            untagged.add(text)
        for t in clean:
            per_tag[t].add(text)
    return per_tag, untagged, cap(problems, 4, f"malformed {role} rows")


def orphan_tag_problems(per_tag, known, role):
    """A tag naming no declared target is almost always a typo, and it is
    the silent kind: the query sinks into a bucket nothing reads, crediting
    neither the intended target nor anything else. Name it (§12.0)."""
    known_keys = {str(k).strip().lower() for k in known}
    orphans = sorted(t for t in per_tag if t not in known_keys)
    return cap([f"{role} queries tagged {t!r}, which is not a declared "
                "target — typo? those queries credit nothing" for t in orphans],
               3, f"orphan {role} tags")


def count_checks(per_tag, untagged, target, names=()):
    """Distinct queries that count for a target (§12.0).

    A tagged query counts for the targets it declares. An UNTAGGED query
    counts for any target its id or name appears in, since the text is then
    the only statement of intent available. A query tagged for something
    else is that target's evidence, not this one's — otherwise an unrelated
    query mentioning "f1 score" certifies facet F1's proof-of-search.
    Ids match exactly; names tolerate plurals, because "gans for tabular
    data" is a better alias search than "gan", not a worse one.
    """
    key = str(target).strip().lower()
    hits = set(per_tag.get(key, set()))
    pats = []
    if isinstance(target, str) and target.strip():
        pats.append(re.compile(r"(?<![a-z0-9_])"
                               + re.escape(target.strip().lower())
                               + r"(?![a-z0-9_])"))
    for n in names:
        for one in (n if isinstance(n, (list, tuple)) else [n]):
            pat = term_pattern(one)
            if pat:
                pats.append(pat)
    for t in untagged:
        if any(p.search(t) for p in pats):
            hits.add(t)
    return len(hits)


def strongly_refuted(contradicting, cards_by_id, author_counts):
    """The shared corroboration bar for the strongest negative verdicts
    (§23.4 overturned, §23.5 refuted, §23.6 failed): two contradicting
    cards, or one at high reliability."""
    if len(contradicting) >= 2:
        return True
    return any(reliability(cards_by_id[e["node"]], author_counts)
               >= OVERTURN_RELIABILITY_MIN for e in contradicting)


def evidence_on_disk(entries, cards_by_id, owner, label, problems):
    """Split evidence into on-disk entries and gate problems for the rest.

    A fabricated citation is the worst thing a run can produce (§1.4);
    silently filtering it out lets it flow into the report unflagged. Shape
    is validated at the boundary (as_evidence), so anything arriving here
    is already an object list."""
    ok = []
    for e in as_evidence(entries, owner, problems):
        node = e.get("node")
        if node in cards_by_id:
            ok.append(e)
        else:
            problems.append(f"{owner}: {label} node {node!r} not on disk")
    return ok


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
    doc, problems0 = read_json_object(os.path.join(root, "state",
                                                   "refresh_sweep.json"))
    n_queries = distinct_role_queries(root, "refresh")
    problems = problems0
    total = as_count(doc.get("base_core_nodes_total"),
                     "refresh_sweep.base_core_nodes_total", problems,
                     allow_missing=True)
    checked = as_count(doc.get("base_core_nodes_checked"),
                       "refresh_sweep.base_core_nodes_checked", problems,
                       allow_missing=True)
    if not doc:
        problems.append("state/refresh_sweep.json not written")
    if not doc.get("completed"):
        problems.append("completed is not true")
    if not total:
        problems.append("base_core_nodes_total missing or 0")
    elif (checked or 0) < total:
        problems.append(f"checked {checked or 0} of {total} base core nodes")
    if n_queries < REFRESH_QUERIES_MIN:
        problems.append(f"{n_queries} distinct role:\"refresh\" queries "
                        f"logged (need {REFRESH_QUERIES_MIN})")
    detail = {"checked": checked, "total": total,
              "refresh_queries": n_queries,
              "new_threats_found": doc.get("new_threats_found"),
              "problems": problems}
    return not problems, detail


def author_presence_counts(cards):
    """How often each author appears across the corpus. A corpus-internal
    standing signal: unbiased by any hardcoded prestige list, computable
    offline, and it measures presence in THIS neighborhood, which is the
    neighborhood that matters."""
    counts = Counter()
    for c in cards:
        for a in (c.get("bib") or {}).get("authors", []) or []:
            key = str(a).strip().lower()
            if key:
                counts[key] += 1
    return counts


def reliability(card, author_counts=None):
    """§23.4 — per-card source reliability in (0, 1].

    Orders descent triage and sets the corroboration bar for strong verdicts
    (an `overturned` fate). NEVER used to exclude a source or silence a
    dispute — banned behavior 46."""
    best = 0.0
    for cl in (card or {}).get("claims", []) or []:
        w = STRENGTH_W.get(cl.get("strength"), 0.3) * \
            EVIDENCE_W.get(cl.get("evidence_type"), 0.6)
        best = max(best, w)
    ev = best if best > 0 else 0.5

    bib = (card or {}).get("bib") or {}
    ext = bib.get("external_citations") or {}
    count = ext.get("count")
    cite = min(1.0, math.log10(count + 1) / 5) if isinstance(count, int) else 0.3

    kind = (card or {}).get("artifact_kind")
    if kind in ("repo", "blogpost"):
        venue = 0.35
    elif bib.get("venue") and bib.get("publication_date"):
        venue = 1.0
    else:
        venue = 0.5

    ap = 0.4
    if author_counts:
        if any(author_counts.get(str(a).strip().lower(), 0) >= 2
               for a in bib.get("authors", []) or []):
            ap = 1.0

    return round(0.40 * ev + 0.30 * cite + 0.15 * venue + 0.15 * ap, 3)


def descent_coverage(root, cards_by_id, mode_doc):
    """§23.4 — the descent tree is ledgered, not sampled.

    Every generation-1 citer triaged, every 'digested' row's card actually on
    disk, and enough distinct descent queries to show the forward sweep
    happened. Fail-closed like everything else."""
    problems = []
    subject = mode_doc.get("subject") or {}
    card_id = subject.get("card_id")
    if not card_id:
        problems.append("subject.card_id not set in state/mode.json")
    else:
        card = cards_by_id.get(card_id)
        if card is None:
            problems.append(f"no card on disk for subject {card_id!r}")
        elif (card.get("provenance") or {}).get("depth") != "full_text":
            problems.append(f"subject card {card_id!r} is not full_text depth")

    descent_dir = os.path.join(root, "state", "descent")
    gen1 = read_jsonl(os.path.join(descent_dir, "generation_1.jsonl"))
    generations = sorted(n for n in (os.listdir(descent_dir)
                                     if os.path.isdir(descent_dir) else [])
                         if n.startswith("generation_"))
    if not gen1:
        problems.append("state/descent/generation_1.jsonl missing or empty")
        triaged_frac = 0.0
    else:
        triaged = [r for r in gen1
                   if r.get("triage") in ("digested", "periphery", "irrelevant")]
        triaged_frac = len(triaged) / len(gen1)
        if triaged_frac < DESCENT_TRIAGE_MIN:
            problems.append(f"only {triaged_frac:.0%} of generation-1 rows "
                            f"triaged (need >={DESCENT_TRIAGE_MIN:.0%})")
        undigested = [r for r in gen1 if r.get("triage") == "digested"
                      and r.get("card_id") not in cards_by_id]
        if undigested:
            problems.append(f"{len(undigested)} generation-1 rows marked "
                            "digested without a card on disk")

    n_queries = distinct_role_queries(root, "descent")
    if n_queries < DESCENT_QUERIES_MIN:
        problems.append(f"{n_queries} distinct role:\"descent\" queries logged "
                        f"(need {DESCENT_QUERIES_MIN})")

    detail = {"subject_card": card_id, "generation_1_rows": len(gen1),
              "triaged_fraction": round(triaged_frac, 3),
              "generations_present": generations,
              "descent_queries": n_queries, "problems": problems}
    return not problems, detail


def claim_coverage(root, cards_by_id, mode_doc, author_counts):
    """§23.4 — every claim carries an evidence-backed fate.

    `ignored` requires proof of search; `overturned` requires corroboration
    scaled by source reliability. A fate the validator cannot trace to cards
    on disk is an assertion, and assertions do not pass gates."""
    problems = []
    claims = as_id_list(mode_doc.get("claims"), "mode.json claims", problems)
    fates, fp = read_json_object(os.path.join(root, "state",
                                              "claim_fates.json"))
    problems.extend(fp)
    if not claims:
        problems.append("mode.json claims is empty; fill it after P0")

    per_tag, untagged, qp = role_target_checks(root, "claim_check")
    problems.extend(qp)
    problems.extend(orphan_tag_problems(per_tag, claims, "claim_check"))

    detail_claims = {}
    for cl in claims:
        f = as_dict(fates.get(cl), f"claim_fates.json[{cl}]", problems)
        fate = f.get("fate")
        ev = evidence_on_disk(f.get("evidence"), cards_by_id, cl,
                              "evidence", problems)
        contradicting = [e for e in ev
                         if e.get("relation") == "contradicts"]
        checks = count_checks(per_tag, untagged, cl)
        if not fate:
            problems.append(f"{cl}: no fate recorded in claim_fates.json")
        elif fate not in CLAIM_FATES:
            problems.append(f"{cl}: fate {fate!r} not in the §23.4 taxonomy")
        elif fate == "ignored":
            if checks < CLAIM_CHECK_QUERIES_MIN:
                problems.append(f"{cl}: ignored with {checks} distinct "
                                f"claim_check queries "
                                f"(need {CLAIM_CHECK_QUERIES_MIN})")
        elif fate == "overturned":
            if not strongly_refuted(contradicting, cards_by_id,
                                    author_counts):
                problems.append(f"{cl}: overturned needs >=2 CONTRADICTING "
                                "cards, or 1 with reliability >= "
                                f"{OVERTURN_RELIABILITY_MIN} "
                                f"(has {len(contradicting)} contradicting)")
        elif fate == "abandoned":
            if not ev and not f.get("timeline"):
                problems.append(f"{cl}: abandoned needs evidence or a dated "
                                "timeline entry")
        else:
            if not ev:
                problems.append(f"{cl}: fate {fate!r} without evidence on disk")
        detail_claims[cl] = {"fate": fate, "evidence_n": len(ev),
                             "claim_checks": checks}

    detail = {"claims": detail_claims, "fates_file_present": bool(fates),
              "problems": problems[:12]}
    return not problems, detail


def resolution_coverage(root, cards_by_id, mode_doc):
    """§23.5 — memory proposes the resolution; only retrieval keeps it.

    Every sense confirmed by retrieved uses, every alias actually searched,
    the origin on disk with a date. An unresolved concept fails closed."""
    problems = []
    res, rp = read_json_object(os.path.join(root, "state",
                                            "concept_resolution.json"))
    problems.extend(rp)
    if not res and not problems:
        problems.append("state/concept_resolution.json not written (P0)")
    queries = [str(q.get("query") or "").lower()
               for q in read_jsonl(os.path.join(root, "state",
                                                "seen_queries.jsonl"))]
    senses = res.get("senses") or []
    if res and not senses:
        problems.append("no senses recorded — what does the term denote?")
    for s in senses:
        sid = s.get("id", "?")
        confirmed = []
        for c in s.get("confirmed_by") or []:
            if c in cards_by_id:
                confirmed.append(c)
            else:
                problems.append(f"sense {sid}: confirming card {c!r} not "
                                "on disk")
        if len(confirmed) < SENSE_CONFIRM_MIN:
            problems.append(f"sense {sid}: {len(confirmed)} confirming cards "
                            f"on disk (need {SENSE_CONFIRM_MIN})")
    for alias in as_id_list(res.get("aliases"), "aliases", problems):
        pat = term_pattern(alias)
        n = sum(1 for q in queries if pat and pat.search(q))
        if n < ALIAS_QUERIES_MIN:
            problems.append(f"alias {alias!r} in {n} logged queries "
                            f"(need {ALIAS_QUERIES_MIN})")
    origin = (res.get("origin") or {}).get("earliest_known") or {}
    if not origin.get("node"):
        problems.append("origin.earliest_known.node not set")
    elif origin["node"] not in cards_by_id:
        problems.append(f"origin node {origin['node']!r} not on disk")
    elif not origin.get("date"):
        problems.append("origin.earliest_known.date missing — priority "
                        "cannot be judged")
    detail = {"senses": len(senses), "aliases": len(res.get("aliases") or []),
              "origin": origin.get("node"), "problems": problems[:10]}
    return not problems, detail


def neighborhood_coverage(root, cards_by_id, mode_doc):
    """§23.5 — a concept is only understood relative to its alternatives."""
    problems = []
    res, rp = read_json_object(os.path.join(root, "state",
                                            "concept_resolution.json"))
    problems.extend(rp)
    sibs = res.get("siblings") or []
    queries = [str(q.get("query") or "").lower()
               for q in read_jsonl(os.path.join(root, "state",
                                                "seen_queries.jsonl"))]
    if not sibs:
        just = (res.get("siblings_none_justification") or "").strip()
        if len(just) < 30:
            problems.append("no siblings and no substantive "
                            "siblings_none_justification — a concept with no "
                            "neighborhood is rare enough to argue for")
    elif len(sibs) < SIBLINGS_MIN:
        problems.append(f"{len(sibs)} sibling(s) mapped "
                        f"(need {SIBLINGS_MIN}, or a justification)")
    for s in sibs:
        name = s.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"sibling entry has no usable name: {s!r}")
            continue
        cards = []
        for c in s.get("cards") or []:
            if c in cards_by_id:
                cards.append(c)
            else:
                problems.append(f"sibling {name!r}: card {c!r} not on disk")
        if len(cards) < SIBLING_CARDS_MIN:
            problems.append(f"sibling {name!r}: {len(cards)} digested cards "
                            f"on disk (need {SIBLING_CARDS_MIN})")
        pat = term_pattern(name)
        n = sum(1 for q in queries if pat and pat.search(q))
        if n < ALIAS_QUERIES_MIN:
            problems.append(f"sibling {name!r} in {n} logged queries "
                            f"(need {ALIAS_QUERIES_MIN})")
    detail = {"siblings": [s.get("name") for s in sibs],
              "problems": problems[:10]}
    return not problems, detail


def property_coverage(root, cards_by_id, mode_doc, author_counts):
    """§23.5 — what the name's reputation rests on, graded not assumed.

    `folklore` carries proof of search; `replicated` requires disjoint author
    groups (popularity is never evidence — banned behavior 48)."""
    problems = []
    facets = as_id_list(mode_doc.get("facets"), "mode.json facets", problems)
    ev_doc, ep = read_json_object(os.path.join(root, "state",
                                               "property_evidence.json"))
    problems.extend(ep)
    if not facets:
        problems.append("mode.json facets is empty; fill it after P0")

    per_tag, untagged, qp = role_target_checks(root, "property_check")
    problems.extend(qp)
    problems.extend(orphan_tag_problems(per_tag, facets, "property_check"))

    detail_facets = {}
    for fid in facets:
        rec = as_dict(ev_doc.get(fid), f"property_evidence.json[{fid}]",
                      problems)
        status = rec.get("status")
        ev = evidence_on_disk(rec.get("evidence"), cards_by_id, fid,
                              "evidence", problems)
        confirming = [e for e in ev if e.get("relation") != "contradicts"]
        contradicting = [e for e in ev if e.get("relation") == "contradicts"]
        checks = count_checks(per_tag, untagged, fid)
        if not status:
            problems.append(f"{fid}: no status in property_evidence.json")
        elif status not in PROPERTY_STATUSES:
            problems.append(f"{fid}: status {status!r} not in the §23.5 "
                            "taxonomy")
        elif status == "replicated":
            groups = [card_authors(cards_by_id.get(e["node"]))
                      for e in confirming]
            if max_disjoint_groups(groups) < 2:
                problems.append(f"{fid}: replicated needs >=2 confirming "
                                "cards with disjoint author lists")
        elif status == "demonstrated":
            if not confirming:
                problems.append(f"{fid}: demonstrated without a confirming "
                                "card on disk")
        elif status == "contested":
            if not contradicting:
                problems.append(f"{fid}: contested without a contradicting "
                                "card on disk")
        elif status == "refuted":
            if not strongly_refuted(contradicting, cards_by_id, author_counts):
                problems.append(f"{fid}: refuted needs >=2 contradicting "
                                "cards or 1 at reliability >= "
                                f"{OVERTURN_RELIABILITY_MIN}")
        elif status == "folklore":
            if checks < PROPERTY_CHECK_QUERIES_MIN:
                problems.append(f"{fid}: folklore with {checks} distinct "
                                "property_check queries (need "
                                f"{PROPERTY_CHECK_QUERIES_MIN})")
        detail_facets[fid] = {"status": status, "evidence_n": len(ev),
                              "checks": checks}
    detail = {"facets": detail_facets, "problems": problems[:12]}
    return not problems, detail


def solution_coverage(root, cards_by_id, mode_doc, author_counts, components):
    """§23.6 — every requirement covered or declared unsolved with proof of
    search; every solution's validity backed by evidence on disk; adoption
    numbers cross-checked, never taken on the agent's word.

    Validity and adoption are separate axes by design (banned behavior 49).
    Validity independence is computed from confirmations+reuses; the
    adoption ceiling additionally counts the `adopters` evidence list, so
    'popular but unvalidated' — the mode's headline case — is recordable."""
    problems = []
    reqs = as_id_list(mode_doc.get("requirements"),
                      "mode.json requirements", problems)
    doc, dp = read_json_object(os.path.join(root, "state", "solutions.json"))
    problems.extend(dp)
    sols = as_dict(doc.get("solutions"), "solutions.json solutions", problems)
    unsolved = as_id_list(doc.get("unsolved_requirements"),
                          "unsolved_requirements", problems)
    if not reqs:
        problems.append("mode.json requirements is empty; fill it after P0")
    if not sols:
        problems.append("state/solutions.json has no solutions recorded")
    if reqs and set(reqs) != set(components):
        only_mode = sorted(set(reqs) - set(components))
        only_graph = sorted(set(components) - set(reqs))
        problems.append("mode.json requirements and graph.meta.components "
                        f"differ -- only in mode.json: {only_mode}; only in "
                        f"graph: {only_graph}. They are the same list (§23.6); "
                        "a requirement missing from the graph escapes the "
                        "corpus floors.")

    per_tag, untagged, qp = role_target_checks(root, "solution_check")
    problems.extend(qp)

    def indep_of(own, evidence):
        """Independent groups among evidence: pairwise-disjoint author sets,
        disjoint from `own`. Fail-closed on an unknown `own` — an authorless
        defining card cannot establish independence, so it grants none."""
        if not own:
            return 0
        return max_disjoint_groups(
            [g for g in (card_authors(cards_by_id.get(e.get("node")))
                         for e in evidence) if g and not (g & own)])

    covered = set()
    detail_sols = {}
    for sid, s in sols.items():
        s = as_dict(s, f"solutions.json[{sid}]", problems)
        node = s.get("node")
        own = card_authors(cards_by_id.get(node))
        authors_known = bool(own)
        if node not in cards_by_id:
            problems.append(f"{sid}: defining node {node!r} not on disk")
        elif not authors_known:
            problems.append(f"{sid}: defining node {node!r} has no "
                            "bib.authors -- independence cannot be "
                            "established; record the authors or maintainers")
        for r, level in as_dict(s.get("covers"), f"{sid} covers",
                                problems).items():
            if r not in reqs:
                problems.append(f"{sid}: covers key {r!r} is not a declared "
                                "requirement in mode.json")
            elif level not in ("full", "partial", "none"):
                problems.append(f"{sid}: covers[{r}]={level!r} is not "
                                "full|partial|none")
            elif level in ("full", "partial"):
                covered.add(r)
        v = as_dict(s.get("validity"), f"{sid} validity", problems)
        status = v.get("status")
        conf = evidence_on_disk(v.get("confirmations"), cards_by_id, sid,
                                "confirmation", problems)
        reuse = evidence_on_disk(v.get("reuses"), cards_by_id, sid,
                                 "reuse", problems)
        refs = evidence_on_disk(v.get("refutations"), cards_by_id, sid,
                                "refutation", problems)
        support = conf + reuse
        indep = indep_of(own, support)
        if not status:
            problems.append(f"{sid}: no validity.status recorded")
        elif status not in SOLUTION_STATUSES:
            problems.append(f"{sid}: status {status!r} not in the §23.6 "
                            "taxonomy")
        elif status == "proven":
            if authors_known and indep < PROVEN_INDEPENDENT_GROUPS_MIN:
                problems.append(f"{sid}: proven needs "
                                f">={PROVEN_INDEPENDENT_GROUPS_MIN} "
                                f"independent groups (computed {indep})")
        elif status == "promising":
            if not support:
                problems.append(f"{sid}: promising without a confirming card "
                                "on disk")
        elif status == "contested":
            if not support or not refs:
                problems.append(f"{sid}: contested needs both confirming and "
                                "refuting cards on disk")
        elif status == "failed":
            if not strongly_refuted(refs, cards_by_id, author_counts):
                problems.append(f"{sid}: failed needs >=2 refuting cards or "
                                "1 at reliability >= "
                                f"{OVERTURN_RELIABILITY_MIN}")
        elif status == "unvalidated":
            checks = count_checks(per_tag, untagged, sid,
                                  names=(s.get("name"),))
            if checks < SOLUTION_CHECK_QUERIES_MIN:
                problems.append(f"{sid}: unvalidated with {checks} distinct "
                                "solution_check queries (need "
                                f"{SOLUTION_CHECK_QUERIES_MIN})")
        adoption = as_dict(s.get("adoption"), f"{sid} adoption", problems)
        adopters = evidence_on_disk(adoption.get("adopters"), cards_by_id,
                                    sid, "adopter", problems)
        adoption_ceiling = (indep_of(own, support + adopters) if adopters
                            else indep)
        # Fail-closed: both axes are explicit (§23.6). An unrecorded or
        # unusable adoption count is not a passing one — zero is honest.
        rec = as_count(adoption.get("adopter_groups"),
                       f"{sid}: adoption.adopter_groups (both axes must be "
                       "explicit; 0 is honest, §23.6)", problems)
        if rec is not None and authors_known and rec > adoption_ceiling:
            problems.append(f"{sid}: adopter_groups={rec} overstates the "
                            f"{adoption_ceiling} independent groups "
                            "computable from evidence on disk (add the "
                            "adopting works to adoption.adopters)")
        org = as_dict(adoption.get("org_backing"),
                      f"{sid} org_backing", problems)
        if org.get("active") and org.get("evidence_node") not in cards_by_id:
            problems.append(f"{sid}: org_backing.active without an evidence "
                            "node on disk -- backing is behavior, not brand")
        detail_sols[sid] = {"status": status, "independent_groups": indep,
                            "adoption_ceiling": adoption_ceiling,
                            "refutations": len(refs)}

    for r in unsolved:
        if r not in reqs:
            problems.append(f"unsolved entry {r!r} is not a declared "
                            "requirement in mode.json -- typo or phantom")
    for r in reqs:
        if r in covered and r in unsolved:
            problems.append(f"{r}: both covered by a solution and declared "
                            "unsolved -- pick one")
        elif r not in covered and r not in unsolved:
            problems.append(f"{r}: no solution covers it and it is not "
                            "declared unsolved")
        elif r in unsolved:
            checks = count_checks(per_tag, untagged, r)
            if checks < SOLUTION_CHECK_QUERIES_MIN:
                problems.append(f"{r}: unsolved with {checks} distinct "
                                "solution_check queries (need "
                                f"{SOLUTION_CHECK_QUERIES_MIN})")

    problems.extend(orphan_tag_problems(per_tag, list(sols) + reqs,
                                        "solution_check"))
    shown = cap(problems, 30, "problems")
    detail = {"solutions": detail_sols, "unsolved": unsolved,
              "requirements_covered": sorted(covered & set(reqs)),
              "problems": shown}
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
    mode, mode_doc, mode_problem = load_mode(root)
    if mode_problem:
        print(f"[graph_metrics] WARNING: {mode_problem}", file=sys.stderr)
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
    cards_by_id = {c.get("id"): c for c in cards if c.get("id")}
    author_counts = author_presence_counts(cards)
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
                                              start_round=prospector_start,
                                              cards_by_id=cards_by_id)
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
    # §12.1 fail-closed: state the gates read must be readable. A corrupt
    # mode declaration or an unparseable log row means the metrics describe
    # something other than what the operator thinks they describe.
    state_problems = ([mode_problem] if mode_problem else []) + jsonl_problems()
    if state_problems:
        gates["state_readable"] = {"value": state_problems[:6],
                                   "threshold": "mode.json valid; every log "
                                                "row an object",
                                   "pass": False}
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
        ac_ok, ac_detail = anchor_coverage(root, cards_by_id, mode_doc)
        gates["anchor_coverage"] = {"value": ac_detail,
                                    "threshold": "anchor digested full_text + "
                                                 f">={ANCHOR_QUERIES_MIN} forward queries",
                                    "pass": ac_ok}
    elif mode == "retrospective":
        dc_ok, dc_detail = descent_coverage(root, cards_by_id, mode_doc)
        gates["descent_coverage"] = {"value": dc_detail,
                                     "threshold": "subject full_text + gen-1 "
                                                  f"triaged >={DESCENT_TRIAGE_MIN:.0%} + "
                                                  f">={DESCENT_QUERIES_MIN} descent queries",
                                     "pass": dc_ok}
        cc_ok, cc_detail = claim_coverage(root, cards_by_id, mode_doc,
                                          author_counts)
        gates["claim_coverage"] = {"value": cc_detail,
                                   "threshold": "every claim: evidence-backed "
                                                "fate per §23.4 minimums",
                                   "pass": cc_ok}
    elif mode == "concept":
        rs_ok, rs_detail = resolution_coverage(root, cards_by_id, mode_doc)
        gates["resolution_coverage"] = {
            "value": rs_detail,
            "threshold": f"every sense >={SENSE_CONFIRM_MIN} cards, every "
                         f"alias >={ALIAS_QUERIES_MIN} queries, origin on disk",
            "pass": rs_ok}
        nb_ok, nb_detail = neighborhood_coverage(root, cards_by_id, mode_doc)
        gates["neighborhood_coverage"] = {
            "value": nb_detail,
            "threshold": f">={SIBLINGS_MIN} siblings x "
                         f">={SIBLING_CARDS_MIN} cards, or justified absence",
            "pass": nb_ok}
        pc_ok, pc_detail = property_coverage(root, cards_by_id, mode_doc,
                                             author_counts)
        gates["property_coverage"] = {
            "value": pc_detail,
            "threshold": "every facet: graded status per §23.5 minimums",
            "pass": pc_ok}
    elif mode == "problem":
        sc_ok, sc_detail = solution_coverage(root, cards_by_id, mode_doc,
                                             author_counts, components)
        gates["solution_coverage"] = {
            "value": sc_detail,
            "threshold": "every requirement covered or unsolved-with-proof; "
                         "every status per §23.6 minimums; adoption "
                         "cross-checked",
            "pass": sc_ok}
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
    if "descent_coverage" in gates and not gates["descent_coverage"]["pass"]:
        guidance.append({"gate": "descent_coverage",
                         "action": "triage_descent_generation",
                         "targets": gates["descent_coverage"]["value"]["problems"][:4]})
    if "claim_coverage" in gates and not gates["claim_coverage"]["pass"]:
        guidance.append({"gate": "claim_coverage",
                         "action": "adjudicate_claim_fates",
                         "targets": gates["claim_coverage"]["value"]["problems"][:4]})
    for cg, action in (("resolution_coverage", "confirm_concept_resolution"),
                       ("neighborhood_coverage", "digest_sibling_concepts"),
                       ("property_coverage", "grade_property_evidence"),
                       ("solution_coverage", "adjudicate_solutions")):
        if cg in gates and not gates[cg]["pass"]:
            guidance.append({"gate": cg, "action": action,
                             "targets": gates[cg]["value"]["problems"][:4]})

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
        "mode_problem": mode_problem,
        "malformed_log_rows": jsonl_problems(),
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

    # §0.1 tamper anchor: hash the sealed file WITHOUT reading its text.
    # recall_check.py compares against the earliest recorded value at P4.
    sealed_path = os.path.join(root, "SEALED_recall_check.md")
    sealed_sha = None
    if os.path.exists(sealed_path):
        h = hashlib.sha256()
        with open(sealed_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        sealed_sha = h.hexdigest()

    ledger_line = {
        "round": rnd,
        "mode": mode,
        "sealed_sha256": sealed_sha,
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
