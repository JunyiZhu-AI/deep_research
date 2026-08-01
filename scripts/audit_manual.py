#!/usr/bin/env python3
"""
audit_manual.py -- runs N genuinely independent audits of the operating manual
through the Anthropic API, then cross-references them.

Independence is the whole point. Three critiques from one context are
correlated and will miss the same things; that is the failure mode the manual's
own §13.1 warns about. Each auditor here gets its own request, its own context,
a different mandate, and no sight of the others. Only the cross-reference pass
sees all of them.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python3 audit_manual.py --manual deep-research-operating-manual.md
    python3 audit_manual.py --manual m.md --scripts scripts/ --rounds 2
    python3 audit_manual.py --manual m.md --model claude-opus-5 --out audit/

Writes:
    audit/auditor_<role>.md        each independent audit
    audit/cross_reference.md       agreements, disagreements, and blind spots
    audit/findings.json            structured, deduplicated

Concurrency respects the same discipline the manual asks for: bounded, with
backoff. Default 3 auditors, max 10.
"""

import argparse
import concurrent.futures as futures
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"

COMMON = """You are auditing an operating manual for a long-running autonomous
research agent. The manual instructs an LLM to spend 10+ hours assessing whether
a research idea is novel, mapping its literature into a graph, and identifying
open directions in the domain.

Context you need:
- The operator has effectively unlimited token budget and 10 concurrent workers.
- The agent has web search, PDF fetch and parsing, shell, and a filesystem. It
  has NO scholarly API (no Semantic Scholar, no OpenAlex) unless keyless
  endpoints happen to be reachable.
- The manual's central design bet is that models are trained toward frugality
  and will under-search unless effort is made mechanically verifiable. Its
  gates are meant to be computed, not asserted.
- The reader of the final report is a senior ML researcher auditing their own
  idea before committing months to it.

Your job is to find what is WRONG with it. Specifically:
- Concrete defects, not general observations.
- For each finding: what breaks, how it would manifest in a real run, and how
  serious it is.
- Rate severity: CRITICAL (produces false conclusions), MAJOR (wastes
  substantial effort or hides a gap), MINOR (friction).
- If you think a design decision is correct but under-justified, say so
  separately -- do not pad the defect list with those.

Do not summarize the manual back. Do not praise it. Assume the author wants the
harshest accurate reading and has no ego investment.

Output format: a markdown list. Each finding starts with a line
`### [SEVERITY] <short title>` followed by: what breaks, how it manifests, and
a suggested fix. End with a section `## What I could not evaluate` naming the
limits of your own review."""

ROLES = {
    "loophole": """YOUR MANDATE: attack the manual as an adversary who wants to
finish early.

You are a model that has been given this manual and wants to stop after two
hours while passing every check. Find every way to satisfy the letter of the
gates while doing a fraction of the work. Walk each of the eleven gates in §12
and ask: what is the cheapest behaviour that makes this metric pass? Which
metrics measure the agent's own choices rather than the world? Which can be
satisfied by looking in a place you have already looked?

Be specific and mechanical. "The agent might not try hard" is useless. "Gate X
passes if the agent issues one weak query under strategy 14, because exhaustion
is defined as zero yield on last use" is what I want.""",

    "methodology": """YOUR MANDATE: audit the measurements and the inferential
claims as a research methodologist would.

Do the metrics measure what they claim? Are any of them confounded with each
other, so that two gates passing is not independent evidence? Is the novelty
rubric sound -- can a component-wise 0-4 scale support the conclusions drawn
from it? Are the cross-paper comparisons valid (benchmark numbers reported by
different papers under different protocols)? Is the saturation logic
statistically meaningful, or is it a heuristic dressed as a measurement? Where
does the design confuse internal consistency with external coverage?

Consider also: all three adjudication passes in §13.1 read the SAME graph. What
errors survive that design?""",

    "operations": """YOUR MANDATE: audit this as the engineer who has to keep a
10-16 hour unattended run alive.

Walk the run end to end and find where it dies, stalls, corrupts state, or
silently degrades. Consider: orchestrator context growth over 20 rounds; HTTP
volume and what a real host's rate limits do to it; disk usage; what happens
when a worker hangs or returns malformed output; whether the resume path in §18
would actually work or has never been exercised; atomicity of writes; whether
concurrency 10 is achievable given per-host limits; what an account-level rate
limit does to a design that assumes unlimited tokens.

Also: what is specified but never verified? A rule nobody checks is a rule that
will be broken by hour six.""",
}

CROSS = """Below are {n} independent audits of the same document, written by
reviewers who could not see each other's work.

Your job is NOT to summarize them. It is to cross-reference:

1. **Converged findings** -- issues flagged by two or more auditors. These are
   the highest-confidence defects. Note who found each and whether they
   described the same underlying cause or merely the same symptom.
2. **Solo findings worth keeping** -- issues only one auditor raised that you
   judge to be real. Say why the others likely missed it (different mandate, or
   genuine oversight).
3. **Contradictions** -- where auditors disagree about whether something is a
   defect, or about its severity. Do not resolve these by averaging; state the
   disagreement and what evidence would settle it.
4. **Collective blind spots** -- what did NONE of them examine? Look at their
   "What I could not evaluate" sections, and think about what falls between the
   three mandates.
5. **Triage** -- a single prioritized list: what to fix first, and what is
   safe to defer or ignore.

Be concrete. If a finding is wrong, say it is wrong."""


def call(model, system, user, max_tokens=8000, retries=5):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set")
    body = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    for attempt in range(retries):
        req = urllib.request.Request(API, data=body, method="POST", headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": VERSION,
        })
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = json.loads(r.read())
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                wait = (2 ** attempt) + random.random()
                print(f"  [{e.code}] backing off {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            sys.exit(f"API error {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            sys.exit(f"network error: {e}")
    sys.exit("exhausted retries")


def read_scripts(path):
    if not path or not os.path.isdir(path):
        return ""
    parts = []
    for name in sorted(os.listdir(path)):
        if name.endswith(".py"):
            with open(os.path.join(path, name), encoding="utf-8") as fh:
                parts.append(f"\n\n===== {name} =====\n{fh.read()}")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", required=True)
    ap.add_argument("--scripts", default=None,
                    help="directory of supplied scripts to audit alongside the manual")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default="audit")
    ap.add_argument("--rounds", type=int, default=1,
                    help="run each auditor N times; disagreement between runs of "
                         "the SAME role tells you how stable the finding is")
    ap.add_argument("--roles", default=",".join(ROLES))
    args = ap.parse_args()

    with open(args.manual, encoding="utf-8") as fh:
        manual = fh.read()
    scripts = read_scripts(args.scripts)
    payload = (f"# THE MANUAL UNDER AUDIT\n\n{manual}"
               + (f"\n\n# SUPPLIED SCRIPTS UNDER AUDIT\n{scripts}" if scripts else ""))

    roles = [r.strip() for r in args.roles.split(",") if r.strip() in ROLES]
    jobs = [(r, k) for r in roles for k in range(args.rounds)]
    os.makedirs(args.out, exist_ok=True)
    print(f"dispatching {len(jobs)} independent audits "
          f"({len(roles)} roles x {args.rounds}) on {args.model}\n")

    def run(job):
        role, k = job
        t0 = time.time()
        text = call(args.model, COMMON + "\n\n" + ROLES[role], payload)
        suffix = "" if args.rounds == 1 else f"_{k+1}"
        path = os.path.join(args.out, f"auditor_{role}{suffix}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# Audit — {role}{suffix}\n\n_model: {args.model}_\n\n{text}")
        crit = text.count("[CRITICAL]")
        major = text.count("[MAJOR]")
        print(f"  {role}{suffix}: {crit} critical, {major} major "
              f"({time.time()-t0:.0f}s) -> {path}")
        return role + suffix, text

    with futures.ThreadPoolExecutor(max_workers=min(10, len(jobs))) as ex:
        results = list(ex.map(run, jobs))

    print("\ncross-referencing...")
    bundle = "\n\n".join(f"===== AUDITOR: {name} =====\n{text}"
                         for name, text in results)
    cross = call(args.model, CROSS.format(n=len(results)),
                 f"# THE DOCUMENT THEY REVIEWED\n\n{manual[:60000]}\n\n"
                 f"# THE AUDITS\n\n{bundle}", max_tokens=10000)
    xpath = os.path.join(args.out, "cross_reference.md")
    with open(xpath, "w", encoding="utf-8") as fh:
        fh.write(f"# Cross-reference of {len(results)} independent audits\n\n{cross}")

    with open(os.path.join(args.out, "findings.json"), "w", encoding="utf-8") as fh:
        json.dump({"model": args.model, "roles": roles, "rounds": args.rounds,
                   "audits": {n: t for n, t in results}, "cross_reference": cross},
                  fh, indent=2, ensure_ascii=False)

    print(f"\ndone -> {xpath}")
    print("Read the cross-reference first; converged findings are the real ones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
