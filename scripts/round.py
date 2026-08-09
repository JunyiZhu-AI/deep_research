#!/usr/bin/env python3
"""
round.py -- one command per round, so the order is never improvised.

Chains: graph_metrics -> validate_graph -> find_opportunities (round >= 8),
then prints a single consolidated verdict and, more usefully, what the failing
gate says to do next.

The individual scripts are documented in MANUAL.md §21 and can be run alone.
This exists because running them out of order -- computing gates before
surgery, or reading a validator result from before the merge -- produces
confident wrong answers about whether the run may stop.

Usage:
    python3 scripts/round.py --run-root runs/my-idea --round 7
    python3 scripts/round.py --run-root runs/my-idea --round 12 --strict
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script, args, label):
    path = os.path.join(HERE, script)
    if not os.path.exists(path):
        print(f"  [{label}] MISSING: {path}")
        return 2, ""
    p = subprocess.run([sys.executable, path] + args,
                       capture_output=True, text=True)
    if p.stderr.strip():
        print(p.stderr.strip(), file=sys.stderr)
    return p.returncode, p.stdout


def estimate_context(root, rnd):
    """Rough estimate of orchestrator context consumed by run state so far.

    Deliberately an estimate: only the harness knows the true figure. This
    exists so the decision to compact is made against a number rather than a
    fixed cadence that fires whether or not it is needed.
    """
    per_round = 0
    gate = os.path.join(root, "state", f"round_{rnd:02d}", "gate.json")
    if os.path.exists(gate):
        per_round += os.path.getsize(gate)
    per_round += 2048          # ~10 worker receipts
    per_round += 1024          # plan + operator notes
    reasoning = 6000           # orchestrator's own per-round output, chars
    return int((per_round + reasoning) * max(rnd, 1) / 4)


def write_digest(root, rnd, failing, vrc, hints, guidance, opp):
    """state/DIGEST.md -- everything needed to continue, and nothing else.

    Fixed size by construction. This is what makes a context reset free: §19
    already requires the run be resumable from disk, so the orchestrator can
    deliberately drop every prior round and reload this instead of carrying 20
    rounds of history it will never reread.
    """
    def read(path, n=None):
        p = os.path.join(root, path)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        return lines[-n:] if n else lines

    decomp = {}
    dp = os.path.join(root, "state", "decomposition.json")
    if os.path.exists(dp):
        try:
            with open(dp, encoding="utf-8") as fh:
                decomp = json.load(fh)
        except json.JSONDecodeError:
            pass

    notes = read("state/operator_notes.md")
    notes = [n for n in notes if n.startswith("-") and len(n) > 3][-3:]

    L = []
    L.append(f"# RUN DIGEST — round {rnd:02d}")
    L.append("")
    L.append("This file is sufficient to continue the run. If you have just reset")
    L.append("your context, read this, then MANUAL.md, then proceed. Do not")
    L.append("reconstruct earlier rounds — they are on disk and you will not need")
    L.append("them.")
    L.append("")
    L.append("## The idea, decomposed")
    for c in (decomp.get("components") or [])[:8]:
        star = " **[load-bearing]**" if c.get("is_load_bearing") else ""
        L.append(f"- **{c.get('id','?')}**: {str(c.get('statement',''))[:150]}{star}")
    if not decomp.get("components"):
        L.append("- (decomposition.json not written yet — P0 incomplete)")
    L.append("")
    L.append("## Where the run stands")
    L.append(f"- Round {rnd}. Gates failing: "
             f"{', '.join(failing) if failing else 'none'}")
    L.append(f"- Graph validator: {'PASS' if vrc == 0 else 'FAIL — report blocked'}")
    if isinstance(opp, dict):
        L.append(f"- Opportunity candidates: {opp.get('candidates', 0)} across "
                 f"{opp.get('types_available', 0)} types")
    L.append("")
    L.append("## Do next")
    for g in (guidance or [])[:5]:
        tg = ", ".join(str(t if not isinstance(t, dict) else
                           t.get("key") or t.get("id") or t)[:44]
                       for t in (g.get("targets") or [])[:3])
        L.append(f"- **{g['action']}** ({g['gate']}){': ' + tg if tg else ''}")
    for u in (hints.get("undigested_high_centrality") or [])[:4]:
        L.append(f"- digest high-centrality node `{u['id']}`")
    if not guidance and not hints.get("undigested_high_centrality"):
        L.append("- (no computed guidance — check gate.json)")
    L.append("")
    if notes:
        L.append("## Latest operator notes")
        L.extend(notes)
        L.append("")
    L.append("## Where everything else lives")
    L.append("- Gates, full: `state/round_XX/gate.json` (trimmed: `gate_summary.json`)")
    L.append("- Per-round history: `state/ledger.jsonl`")
    L.append("- Frontier queue: `state/frontier.json`")
    L.append("- Corpus registry: `corpus/index.jsonl` · cards: `corpus/cards/`")
    L.append("- Graph: `graph/graph.json` — **never read this whole file**")
    L.append("")
    L.append("## Judgment carried forward")
    L.append("`state/JUDGMENT.md` holds what you concluded that no script can")
    L.append("recompute — which threads feel thin, which names keep recurring,")
    L.append("which leads you distrust and why. Read it alongside this digest.")
    L.append("It is the only part of your state that is genuinely lost if you")
    L.append("drop context without writing it down first.")
    L.append("")
    L.append("Never hold state only in context. If it is not in this digest,")
    L.append("in JUDGMENT.md, or on disk, it is lost.")

    out = "\n".join(L)
    path = os.path.join(root, "state", "DIGEST.md")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(out)
    os.replace(tmp, path)
    print(f"\ndigest written: {path}  ({len(out)/1024:.1f} KB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--strict", action="store_true",
                    help="also run the title/text cross-check on every card")
    args = ap.parse_args()
    root, rnd = args.run_root, args.round

    mode_path = os.path.join(root, "state", "mode.json")
    mode, floors = "fresh", {}
    if os.path.exists(mode_path):
        try:
            with open(mode_path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if doc.get("mode") in ("fresh", "incremental", "anchored",
                                   "retrospective", "concept", "problem"):
                mode = doc["mode"]
                floors = doc.get("floors") or {}
        except json.JSONDecodeError:
            pass
    prospector_start = (floors.get("prospector_start_round", 4)
                        if mode == "incremental" else 8)

    print(f"\n=== round {rnd:02d}"
          + (f"  [{mode} — MANUAL §23]" if mode != "fresh" else "") + " ===\n")

    print("[1/3] metrics + gates")
    rc, out = run("graph_metrics.py", ["--run-root", root, "--round", str(rnd)],
                  "metrics")
    if rc != 0:
        print("  metrics failed; stopping. Fix before continuing.")
        return 2
    try:
        m = json.loads(out)
        led = m.get("ledger", {})
        print(f"  nodes {led.get('nodes_core')} core / {led.get('nodes_total')} total"
              f"  ·  cards {led.get('cards_full_text')} full-text"
              f"  ·  clusters {led.get('clusters')}")
        print(f"  new_node_rate {led.get('new_node_rate')}"
              f"  ·  closure {led.get('citation_closure')}"
              f"  ·  stability {led.get('cluster_stability')}")
        failing = m.get("failing", [])
    except (json.JSONDecodeError, AttributeError):
        print(out[-800:])
        failing = ["<unparsed>"]

    print("\n[2/3] graph validator")
    vargs = ["--run-root", root] + (["--strict-bib"] if args.strict else [])
    vrc, vout = run("validate_graph.py", vargs, "validate")
    for line in vout.splitlines():
        if line.strip().startswith(("[!]", "===", "  blocking")):
            print("  " + line.strip())

    opp = ""
    if rnd >= prospector_start:
        print("\n[3/3] opportunity candidates")
        orc, oout = run("find_opportunities.py", ["--run-root", root], "opps")
        try:
            o = json.loads(oout)
            print(f"  {o['candidates']} candidates across {o['types_available']} types"
                  f"  ·  {o['future_work_themes']} future-work themes")
            for t in o.get("top_themes", [])[:3]:
                print(f"    - {t['theme']}  (support {t['support']}, {t['span']})")
            opp = o
        except (json.JSONDecodeError, KeyError):
            print(oout[-400:])
    else:
        print(f"\n[3/3] opportunity candidates — skipped (prospector starts "
              f"round {prospector_start})")

    # ---- consolidated verdict
    gate_path = os.path.join(root, "state", f"round_{rnd:02d}", "gate.json")
    guidance = []
    if os.path.exists(gate_path):
        with open(gate_path, encoding="utf-8") as fh:
            gate = json.load(fh)
        guidance = gate.get("next_round_guidance", [])
        hints = gate.get("frontier_hints", {})
    else:
        hints = {}

    # gate.json cannot know the validator result (it is computed separately),
    # so its validator entry is always failing. Reconcile it here: this chain
    # just ran validate_graph, and its exit code is the gate.
    if vrc == 0:
        failing = [f for f in failing if f != "validator"]
    ok = (vrc == 0 and not failing)
    print("\n" + "=" * 52)
    print(f"  round {rnd:02d}: " + ("ALL GATES PASS" if ok else "CONTINUE"))
    if failing:
        print(f"  failing gates: {', '.join(failing)}")
    if vrc != 0:
        print("  graph validator: FAIL (report blocked)")
    print("=" * 52)

    if guidance:
        print("\nWhat the failing gates say to do next round:")
        for g in guidance:
            targets = g.get("targets", [])
            shown = ", ".join(str(t if not isinstance(t, dict) else
                                  t.get("key") or t.get("id") or t)[:44]
                              for t in targets[:4])
            print(f"  · {g['action']}  ({g['gate']})")
            if shown:
                print(f"      {shown}")

    und = hints.get("undigested_high_centrality", [])[:5]
    if und:
        print("\nHighest-centrality nodes not yet digested — top frontier priority:")
        for u in und:
            print(f"  · {u['id']}  (pagerank {u['pagerank']})")

    write_digest(root, rnd, failing, vrc, hints, guidance, opp)

    est = estimate_context(root, rnd)
    print(f"\nestimated orchestrator context from run state: ~{est:,} tokens "
          f"after {rnd} rounds")
    if est > 600_000:
        print("-" * 52)
        print("  COMPACTION ADVISED (MANUAL §2.2.2)")
        print("  You are past ~60% of a 1M window. Compact -- do not wipe:")
        print(f"    1. Append what you have learned to state/JUDGMENT.md")
        print(f"    2. Drop rounds older than the last 3 from context")
        print(f"    3. Reload state/DIGEST.md + state/JUDGMENT.md")
        print("  JUDGMENT.md is the part that cannot be rebuilt from disk.")
        print("  Write it BEFORE dropping anything.")
        print("-" * 52)
    elif rnd >= 4 and not os.path.exists(os.path.join(root, "state", "JUDGMENT.md")):
        print("  note: state/JUDGMENT.md does not exist yet. Start it — your "
              "accumulated\n        hunches are the one thing no script can "
              "reconstruct (MANUAL §2.2.3).")

    if ok:
        print("\nGates pass. Before writing the report: run P3 red team to null, "
              "P4 recall check, P5 triple adjudication, P6 evolution + prospect.")
        print("Then: render_viz.py, then validate_report.py must exit 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
