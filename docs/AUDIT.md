# Audit of the Deep Research Operating Manual v1.1

**Status of this document.** These are three passes under deliberately separated
mandates, written in one context. They are *not* three independent agents, and
they are correlated in ways I cannot measure from the inside — the same
assumptions that produced the design produced this critique. Run
`scripts/audit_manual.py` on your own account for the real thing; treat this as
a first pass that narrows what the independent auditors should confirm.

Severity: **CRITICAL** = produces false conclusions. **MAJOR** = wastes
substantial effort or hides a gap. **MINOR** = friction.

---

## Pass A — Loopholes: how a model finishes early while passing every gate

### [CRITICAL] A1. `strategy_exhaustion` is satisfied by deliberate weak queries

The gate defines exhaustion as "strategies yielding zero new relevant items on
last use." Nothing specifies query quality or count. A model that wants to
finish issues one lazy query under each unused strategy, gets nothing, and marks
12 of 15 exhausted in a single round.

This is the single cheapest way to pass the hardest-looking gate in §12, and it
inverts the metric's meaning: a *worse* query makes the gate pass *sooner*.

**Fix:** exhaustion requires (a) ≥8 distinct queries on that strategy's last
use, (b) the last use within the previous 3 rounds, and (c) the strategy having
produced ≥1 new node at some point in the run, or an explicit logged argument
for why it is structurally inapplicable to this idea.

### [CRITICAL] A2. `new_node_rate` measures the agent's choice of where to look

New nodes per digested paper falls naturally when you digest papers already
present in your own reference lists. The agent controls the frontier, so it
controls this metric directly: pick safe, already-linked frontier items for
three rounds and the gate opens. The metric claims to measure "the field is
exhausted" and actually measures "I stopped looking outward."

**Fix:** only count a round toward the streak if that round included ≥2 slots on
underused strategies (the §10 reserve rule exists but is not tied to the gate).
Better: compute new-node rate *separately* for exploratory slots and report both.

### [MAJOR] A3. `redteam_null` has no proof-of-work

Two consecutive rounds with no new ≥medium threat opens the novelty gate.
Nothing verifies the red team searched. It has the same shape as A1: the null
result is the agent's own report about its own effort.

**Fix:** a red-team round is null only if it logged ≥15 distinct queries across
≥4 strategies and produced ≥3 candidates it then rejected with stated reasons. A
round that returns nothing *and* searched nothing is void, not null.

### [MAJOR] A4. Report depth minimums (§15.4) are specified but never checked

"Every core node appears in §9" is mechanically verifiable and nothing verifies
it. §16.20 bans omitting them, but bans without checks are decoration by hour
six.

**Fix:** `validate_report.py` — parse the finished report, confirm every
`status: core` node ID appears in the bibliography, every cluster has a §5
subsection, every component has a §2 paragraph, every opportunity appears in §7.
Make it a delivery gate alongside `validate_graph.py`.

### [MAJOR] A5. Nothing checks that a digest card reflects its paper

`--strict-bib` confirms the title appears in the extracted text. It does not
check that `mechanism`, `claims`, or `per_component` bear any relation to the
document. A worker can fetch a PDF, extract text, and write generically
plausible analysis. The corpus would look complete and be hollow, and every
downstream conclusion would inherit it.

This is the most consequential unchecked assumption in the design.

**Fix:** per-round card spot-audit. Sample 3 cards, hand a verifier the raw text
with the card's `mechanism` field withheld, have it re-derive independently, and
compare. Log agreement rate to the ledger; a sustained low rate is a
run-invalidating condition. Cheap in your budget, and it is the only defense
against a hollow corpus.

### [MINOR] A6. "Round" has no minimum size

12 rounds and 200 papers are compatible with 17-paper rounds. Not exploitable on
its own given the other floors, but it means "12 rounds" carries less
information than it appears to.

---

## Pass B — Methodology: do the measurements mean what they claim?

### [CRITICAL] B1. `citation_closure` rewards a narrow corpus

Closure is computed over references appearing ≥3 times across cards. A tight,
self-referential clique of 60 papers cites itself densely; closure hits 0.95
almost immediately. A broad, honest corpus spanning six communities has many
frequently-cited references it has not yet digested and scores *worse*.

The metric measures internal consistency and is being read as external coverage.
It is arguably backwards: high closure early is evidence of a narrow search, not
a complete one.

**Fix:** condition it. Report closure alongside the number of distinct clusters
and the count of frequent references first encountered via a *different* search
strategy than the citing paper. Or gate on closure only after cluster count
stabilizes above a floor.

### [CRITICAL] B2. Cross-paper benchmark comparison is not valid as specified

§14 says to plot reported numbers over time from card `results` to find
saturation. Papers report different splits, metrics, model scales, and
protocols. Averaging or trending across them produces confident, wrong claims
about a field plateauing.

`find_opportunities.py` inherits this: `evaluation_gaps()` groups by benchmark
name only and compares early-half to late-half means, mixing accuracy with F1 if
both appear. **This is a defect in code I wrote.**

**Fix:** key comparisons on `(benchmark, metric, split)` and require ≥4 points
within one such key before trending. Mark every cross-paper trend as indicative,
never as evidence, and never let one alone generate an `evaluation_gap`.

### [MAJOR] B3. Two saturation gates measure the same thing

`new_node_rate` falling and `cluster_stability` rising are both consequences of
adding few nodes. A round that adds little scores well on both. Passing two
gates reads as independent corroboration and is not — it is one signal counted
twice, and that inflates confidence exactly when the run is slowing for the
wrong reasons.

**Fix:** state the dependence explicitly in §12 so it is not read as
confirmation, and add one genuinely independent signal — e.g. the fraction of
newly digested papers whose reference lists are already fully covered, which
moves for different reasons.

### [MAJOR] B4. Triple-run adjudication shares a corpus, so it shares blind spots

§13.1's three passes are independent of each other and identically dependent on
the graph. Unanimity across them says the corpus supports the verdict; it says
nothing about whether the corpus is complete. The design invites reading
unanimity as high confidence in *novelty*, when it is only high confidence in
*consistency with what was found*.

**Fix:** the report must state the verdict as conditional — "novel with respect
to a corpus of N papers covering clusters X, thin in region Y" — and the §0
headline confidence must be capped by coverage, not by adjudicator agreement.
The sealed recall check is the only external calibration in the design; if it
found a miss, every confidence in the report should be reduced accordingly, and
that linkage is currently unstated.

### [MINOR] B5. The 0–4 novelty scale mixes two dimensions

Score 1 ("anticipated in an adjacent field") and score 2 ("partially
anticipated, different assumptions") are not points on one axis — one is about
*where* the prior art lives, the other about *how completely* it anticipates.
Two components scoring 2 can mean very different things.

**Fix:** either split into two fields (`anticipation_completeness`,
`domain_distance`) or accept the conflation and document it so the report does
not over-read the ordering.

### [MINOR] B6. `future_work` support counts inflate

Support counts entries, not papers; one paper listing three similar limitations
contributes three. `distinct_nodes` is computed but the manual's guidance points
at support. Read `distinct_nodes` and `year_span`.

---

## Pass C — Operations: does it survive 10–16 hours unattended?

### [MAJOR] C1. Account-level rate limits are unmodelled

The design's premise is unlimited tokens. Unlimited tokens is not unlimited
requests, and a corporate API account typically carries per-minute request and
token ceilings that 10 concurrent workers hitting long-context calls will reach.
The run does not degrade gracefully — §2.2.2 handles *host* rate limits for
fetching, not *provider* limits on inference.

**Fix:** worth confirming your account's actual limits before the first run, and
adding a provider-side backoff that reduces concurrency rather than failing the
round. Also worth knowing whether a 10–16 hour unattended job is acceptable use
on your company's account.

### [MAJOR] C2. No worker deadline

If a spawned worker hangs — a slow host, a 300-page PDF, a pathological parse —
the round blocks indefinitely. Nothing specifies a timeout or a rule for
proceeding with 9 of 10.

**Fix:** per-worker deadline (~20 min scout, ~40 min digest), and a rule that a
round proceeds with ≥7 returns, re-queuing the rest. Log timeouts to the ledger.

### [MAJOR] C3. The resume path is specified but never exercised

§18 describes reconstruction from disk. Nothing tests it, and a resume path
first exercised during an actual failure at hour nine will not work.

**Fix:** mandatory resume drill at the end of round 3 — discard in-context
state, reload from disk alone, confirm the frontier and ledger reconstruct, log
the result. If the drill fails, the run stops at round 3 rather than at hour nine.

### [MINOR] C4. `gate.json` is larger than the context budget implies

§2.2 promises the orchestrator reads only receipts and metrics, but
`frontier_hints` carries 25 structural holes, 20 undigested nodes, 20 missing
refs, and a full year histogram, every round. Over 20 rounds this is the largest
single contributor to orchestrator context.

**Fix:** cap `frontier_hints` to the top 10 per category, and write the full
version to a separate file the orchestrator reads only when a specific gate fails.

### [MINOR] C5. Orchestrator merge writes are not specified as atomic

`graph_metrics.py` writes atomically (tmp + `os.replace`). The orchestrator's
own merge step is not required to. An interruption mid-merge loses the graph.

**Fix:** state the tmp-then-rename requirement in §2.2.1, and snapshot before
merge, not only after metrics.

### [MINOR] C6. Disk and fetch volume are unstated

~400 PDFs at ~2 MB plus extracted text and the fetch cache lands around 1–1.5 GB.
Roughly 1,200–1,600 HTTP requests, most to a handful of hosts. Neither is a
problem; both should be stated so they do not surprise, and arXiv in particular
asks for gentler bulk pacing than the manual's 1 req/sec/host default.

---

## Collective blind spots

Things none of the three mandates examined, which the independent auditors
should be pushed toward:

1. **Is the output actually decision-useful?** Every pass audited process
   fidelity. None asked whether a senior researcher reading the finished report
   would make a better decision than after a focused afternoon of manual
   searching. That is the only question that matters, and the design has no
   measurement of it.
2. **Cost per marginal insight.** Rounds 12–20 may contribute almost nothing
   over rounds 1–11. Nobody measured whether the effort curve flattens, and the
   gates are designed to *prevent* stopping, which would mask it.
3. **The decomposition ensemble is unvalidated.** Ten independent
   decompositions merged by union is asserted to improve recall. It might
   mostly produce ten paraphrases of the same three components, in which case
   the ensemble is expensive theater.
4. **Whether the manual is too long to be followed.** ~10,000 words of
   instruction. Adherence to a rule in §16.22 at hour eleven is untested and is
   the load-bearing assumption of the entire design.

---

## Triage

**Fix before the first real run:**
A1, A2 (gates that reward the behavior they exist to prevent) · A5 (hollow-corpus
defense) · B2 (invalid comparisons, including in my code)

**Fix before trusting a verdict:**
B1, B4 (coverage confidence is currently overstated) · A4 (report checking) ·
C1 (know your account limits)

**Fix before an unattended overnight run:**
C2, C3

**Document, don't fix:** B3, B5, B6, C4, C5, C6, A6

The pattern across the highest-severity findings is one thing: **several gates
measure the agent's own reports about its own effort rather than anything
external.** A1, A2, A3, and A5 are all instances. The design assumed
adversarial pressure on *thoroughness* and applied it to search behavior, but
the gates that check that behavior are themselves self-reported. The one
external calibration in the whole system is the sealed recall check, and it runs
once, at the end.

---

# Round 2 audit — v1.2 and the packaged repo

Different lenses from round 1, chosen to avoid re-finding the same things:
**consistency** (does the manual match the code match the README), **bootstrap**
(what breaks when the agent gets only the README), and **regression** (did the
v1.2 fixes work, or only look like they did).

Same caveat as round 1: written in one context by the author of the design.
Run `scripts/audit_manual.py` for the independent version.

## [CRITICAL] R1. The v1.2 fixes changed the documentation, not the enforcement — FIXED

§12.1 stated hardened definitions for `strategy_exhaustion`, `new_node_rate`,
and `redteam_null`. `graph_metrics.py` still implemented the v1.1 loopholes
verbatim. The manual said a strategy needs ≥8 distinct recent queries to count
as exhausted; the code still passed it on one weak query returning nothing.

This is the exact pattern round 1 named — *a rule nobody checks is a rule that
will be broken by hour six* — reproduced by the person who wrote it down. Prose
fixes to a mechanically-enforced system are not fixes.

On the test fixture the difference is stark: 13 of 15 strategies "exhausted" and
a red-team streak of 2 before, versus 0 and 0 after, each with a stated reason.
Both would have opened the stopping gate.

**Fixed.** All three now computed, with rejection reasons surfaced in `gate.json`.

## [CRITICAL] R2. Two of the twelve gates did not exist in code — FIXED

`card_fidelity` and `opportunity_coverage` were added to the §12 table and to
the README's claim in v1.2. Nothing computed them. `gate.json` carried ten
gates, all passing, and the agent would have concluded it could stop — with the
hollow-corpus defense and the prospector coverage check silently absent.

The manual also disagreed with itself: "All twelve must pass" against a delivery
checklist saying "All 11."

**Fixed.** Both computed and fail-closed: no spot-audits logged means
`card_fidelity` fails rather than passes vacuously.

## [MAJOR] R3. The gates read log fields that were never specified — FIXED

All three hardened gates depend on `state/seen_queries.jsonl` carrying `round`,
`strategy`, `role`, the literal `query` string, and `new_relevant`.
`card_fidelity` depends on a `verdict` field in `state/card_audits.jsonl`.
`redteam_null` depends on rejected candidates being logged at all. None of these
schemas appeared anywhere in the manual.

An agent would have written reasonable-looking logs missing the fields, and
every dependent gate would have failed for an unexplained reason — or worse,
been quietly skipped.

**Fixed.** §12.0 now specifies all three schemas and states plainly that a
missing field means the gate fails.

## [MINOR] R4. README overstated the manual's length

Claimed ~12,000 words against 12,838, now ~13,000. Trivial in itself; noted
because the README is one of only two inputs the agent receives, so every claim
in it is load-bearing in a way an ordinary README's is not.

## Verified clean

- Every `§n` reference resolves to a real section after the v1.2 renumbering
  (24 sections after §23 run modes was added, re-checked, no dangling refs).
- Every script named in the README exists and runs.
- The full chain runs end to end from a fresh copy: `init_run` → `round` →
  `validate_graph` → `find_opportunities` → `render_viz` → `validate_report`.
- §16 readability caps do not contradict §15.4 depth floors — they apply to
  different layers, as intended.

## Still open

- **The pattern behind R1 and R2 is not fixed, only its instances.** Nothing
  checks that MANUAL.md and the scripts agree. A conformance test — parse the
  §12 gate table, assert every row has a computed key in `gate.json` — would
  have caught both automatically and does not exist.
- No wall-clock ceiling. The v1.2 gate hardening makes the run strictly longer
  and nothing bounds it.
- Round 1's four collective blind spots stand, in particular: nobody has
  measured whether rounds 12–20 contribute anything over rounds 1–11, and
  whether ~13,000 words of instruction is still followed at hour eleven remains
  the load-bearing untested assumption.
- **The §23 mode gates repeat the A1–A3 pattern at smaller scale.** The
  `refresh_sweep` totals are self-reported; the cross-check only requires ≥10
  distinct `role: "refresh"` queries, which an agent could satisfy with token
  searches while fabricating `base_core_nodes_checked`. `anchor_coverage` has
  the same shape. Proof-of-work narrows the lazy path; it does not close it.
- **Delta scoping trusts agent-written `per_component` entries.** An inherited
  card counts toward the incremental floors the moment a delta entry appears
  on it; only the §12.2 fidelity audit checks that the entry reflects the
  paper. Fidelity sampling should over-weight re-adjudicated inherited cards
  in incremental runs, and currently nothing enforces that.
- **The incremental floors (6 rounds, 75 delta-scoped cards) are judgment, not
  measurement.** No incremental run has been executed. Calibrate the same way
  §0.3 calibrates a fresh run: add a feature whose prior art you already know
  to a completed calibration run, and check the delta red team finds it.
- **The §23.4 reliability weights (0.40/0.30/0.15/0.15) are hand-set and
  uncalibrated**, and the venue signal is a crude binary (published vs not).
  Affiliation was deliberately left out of the computed score — a hardcoded
  prestige list is a filter that fails exactly where this harness hunts — and
  demoted to a qualitative reading-order tiebreaker; if that was the wrong
  call, the place to revisit it is `reliability()` in graph_metrics.py, not
  the fate thresholds.
- **Descent triage is self-reported per row.** The gate cross-checks that
  `digested` rows have cards on disk, but a row marked `irrelevant` is
  trusted. The card-fidelity audit does not currently sample triage verdicts;
  a spot-audit of `irrelevant`/`periphery` rows would close this the same way
  §12.2 closes hollow cards.
- **A claim fate is one judgment over many cards** — the same shape as the P5
  adjudication risk. §23.4 mandates the §13.1 triple-pass for load-bearing
  claims, but nothing mechanical verifies the three passes were independent
  or happened at all — the same enforcement gap R1 documented for §13.1
  itself.
- **The §23.5 concept gates prove search, not understanding.** Alias coverage
  is a substring match over logged queries — a token query containing the
  alias satisfies it without engaging the literature. Sense confirmation and
  sibling cards are agent-tagged lists cross-checked only for existence on
  disk; whether the card actually *uses* that sense is checked by nothing but
  the §12.2 fidelity audit, which does not currently sample for it. The
  disjoint-author test for `replicated` sees author strings only: the same
  group under different spellings, or serial collaborators publishing
  separately, both evade it.
- **Proof-of-search text matching has an irreducible residual.** After the
  second review round, a query tagged for one target no longer text-credits
  a different one, and matching is word-boundary with underscores. What
  remains: an UNTAGGED query whose text incidentally contains a short id
  token ("f1 score" crediting facet F1) still counts. No string mechanism
  can distinguish that from a genuine untagged search; the mitigation is
  behavioral — tag your check queries — and the backstop is the fidelity
  audit, per the accounting-not-judgment rule.
- **The §23.6 solution gate cross-checks structure, not truth.** A
  confirmation card's existence on disk is verified; that it actually
  confirms the solution is not — the same fidelity dependence as everywhere
  else. `covers` is entirely agent-asserted: a solution marked `full` for a
  requirement it barely touches passes every check, and only the §12.2 audit
  or the operator's own reading catches it. The independent-groups counter
  inherits the author-string weaknesses noted for §23.5 `replicated`.
- **recall_check.py grades the seal mechanically, but its matcher can flatter
  in both directions.** Title-token containment at 0.6 can call an item found
  when the corpus merely holds a similarly-titled paper (overstating recall,
  the dangerous direction) — the manual says verify found matches by eye, and
  nothing enforces that. The hash anchor starts at round 1: edits before the
  first `graph_metrics.py` run are invisible, and operator edits are
  indistinguishable from agent edits — a changed hash is a flag to surface,
  not proof of tampering. The gate guard reads `gate.json`, which the agent's
  own logs feed; an agent that fabricated its way past the gates can also
  unseal early.
