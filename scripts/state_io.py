"""The run-state contract: how state files are read, shaped, and matched.

Every gate reads JSON that an agent wrote under time pressure at hour nine.
Three review rounds established the lesson the hard way: validating those
shapes field-by-field inside each gate does not converge, because the space
of wrong shapes is unbounded. So shapes are checked ONCE, here, at the
boundary — a wrong shape becomes a legible gate problem before any gate
logic touches it, and never an AttributeError that kills the round.

This module is also the single home for the things that drifted between
copies: the mode list, the mode loader, and the two matching rules.

Dependency-free (stdlib only) and copied into every run by init_run.py.
"""

import json
import os
import re

# --- run modes (MANUAL §23) -------------------------------------------------

VALID_MODES = ("fresh", "incremental", "anchored", "retrospective",
               "concept", "problem")

# Modes whose §0 is a novelty verdict (recommendation + falsifier wording,
# §16.3). The §23.4-§23.6 modes define their own §0 shapes.
NOVELTY_MODES = ("fresh", "incremental", "anchored")


def load_mode(root):
    """§23.0 — returns (mode, doc, problem).

    `problem` is None or a human-readable sentence. Every caller surfaces it
    the same way, so a bad declaration cannot be loud in one script and
    silent in another. Crucially, an UNREADABLE mode.json is a problem, not
    a fallback: 'absent' means fresh, 'corrupt' means we do not know what
    this run is, and guessing fresh silently would skip every mode gate.
    """
    path = os.path.join(root, "state", "mode.json")
    if not os.path.exists(path):
        return "fresh", {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return "fresh", {}, (f"state/mode.json is unreadable ({exc}); the "
                             "run's mode cannot be determined and every §23 "
                             "mode gate is therefore OFF. Fix the file "
                             "(§23.0).")
    if not isinstance(doc, dict):
        return "fresh", {}, (f"state/mode.json holds a "
                             f"{type(doc).__name__}, not an object; mode "
                             "gates are OFF until it is fixed (§23.0).")
    if "mode" not in doc:
        return "fresh", doc, None
    declared = doc.get("mode")
    if declared not in VALID_MODES:
        return "fresh", doc, (f"declared mode {declared!r} is not one of "
                              f"{VALID_MODES}; the gates are running as "
                              "FRESH, not as declared (§23.0).")
    return declared, doc, None


# --- shape validation -------------------------------------------------------

def read_json_object(path, owner=None):
    """Read a state file that must hold a JSON object.

    Returns (dict, problems). Absent is fine and silent — gates decide
    whether absence matters. Unreadable or wrong-typed is never silent.
    """
    owner = owner or os.path.basename(path)
    if not os.path.exists(path):
        return {}, []
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return {}, [f"{owner}: unreadable ({exc})"]
    if not isinstance(doc, dict):
        return {}, [f"{owner}: expected a JSON object, got "
                    f"{type(doc).__name__}"]
    return doc, []


def read_jsonl_objects(path, owner=None):
    """Read a JSONL log whose rows must be objects.

    Returns (rows, problems). A row that is valid JSON but not an object is
    reported and skipped — never handed to code that will call .get on it.
    """
    owner = owner or os.path.basename(path)
    rows, problems = [], []
    if not os.path.exists(path):
        return rows, problems
    try:
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    problems.append(f"{owner}:{n}: not valid JSON")
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    problems.append(f"{owner}:{n}: row is a "
                                    f"{type(row).__name__}, not an object")
    except OSError as exc:
        problems.append(f"{owner}: unreadable ({exc})")
    return rows, cap(problems, 4, f"malformed rows in {owner}")


def cap(problems, n, label):
    """Keep problem lists legible without hiding their size."""
    if len(problems) <= n:
        return problems
    return problems[:n] + [f"...and {len(problems) - n} more {label}"]


def as_dict(value, owner, problems):
    """A field that must be an object. Anything else is a stated problem."""
    if isinstance(value, dict):
        return value
    if value not in (None, {}):
        problems.append(f"{owner}: expected an object, got "
                        f"{type(value).__name__}")
    return {}


def as_id_list(value, owner, problems):
    """A field that must be a LIST of non-empty id strings.

    Checks list-ness first: a bare string is a list of characters to Python
    and silently becomes phantom ids otherwise. Empty strings are rejected
    because an empty id matches everything downstream.
    """
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        problems.append(f"{owner}: expected a list of id strings, got "
                        f"{type(value).__name__} {value!r}")
        return []
    ok = []
    for x in value:
        if isinstance(x, str) and x.strip():
            ok.append(x.strip())
        else:
            problems.append(f"{owner}: id {x!r} is not a non-empty string")
    return ok


def as_count(value, owner, problems, allow_missing=False):
    """A field that must be a non-negative integer count.

    Returns None when absent (callers decide whether that is allowed) and
    on any bad value — booleans included, since bool is an int in Python
    and `True` would otherwise silently mean 1.
    """
    if value is None:
        if not allow_missing:
            problems.append(f"{owner}: not recorded")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{owner}: {value!r} is not an integer -- a metric "
                        "that cannot be computed counts as failed (§12.1)")
        return None
    if value < 0:
        problems.append(f"{owner}: {value} is negative")
        return None
    return value


def as_evidence(value, owner, problems):
    """An evidence list: entries must be objects carrying a `node` id.

    Bare id strings are the shape a hurried or fabricating agent produces;
    they are reported rather than silently skipped or crashed on, because
    an unchecked citation is the worst thing this run can emit (§1.4).
    """
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        problems.append(f"{owner}: expected a list of evidence objects, got "
                        f"{type(value).__name__}")
        return []
    ok = []
    for e in value:
        if not isinstance(e, dict):
            problems.append(f"{owner}: evidence entry {e!r} is not an object "
                            "with a node id")
            continue
        node = e.get("node")
        if node is not None and not isinstance(node, str):
            problems.append(f"{owner}: evidence node {node!r} is not a string")
            continue
        ok.append(e)
    return ok


def as_node_refs(value, owner, problems):
    """A list of node ids referenced by a record (opportunity evidence).

    Accepts the {"nodes": [...]} shape used by §11.4 records; anything else
    is reported. Never iterates a bare string into characters.
    """
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        problems.append(f"{owner}: expected a list of node ids, got "
                        f"{type(value).__name__}")
        return []
    ok = []
    for n in value:
        if isinstance(n, str) and n.strip():
            ok.append(n.strip())
        else:
            problems.append(f"{owner}: node ref {n!r} is not a string")
    return ok


# --- matching ---------------------------------------------------------------
#
# Two rules, deliberately different, defined once so no caller invents a
# third. IDs match exactly at word boundaries (SOL-1 must not hide inside
# SOL-10). Natural-language terms — aliases, sibling concepts, solution
# names — additionally tolerate a plural suffix, because an agent searching
# an alias well writes "gans for tabular data", and demanding the singular
# would reward worse searching.

_ID_BOUND = r"(?![A-Za-z0-9_])"
_ID_LEAD = r"(?<![A-Za-z0-9_])"


def id_present(needle, hay):
    """Exact, word-boundary id containment. Non-string or empty ids match
    nothing: they are validated at the boundary, and failing closed here is
    correct if one slips through."""
    if not isinstance(needle, str) or not needle.strip():
        return False
    return re.search(_ID_LEAD + re.escape(needle.strip()) + _ID_BOUND,
                     hay) is not None


def term_pattern(term):
    """Case-insensitive, boundary-anchored, plural-tolerant matcher for a
    natural-language term. Returns None for unusable input."""
    if not isinstance(term, str) or not term.strip():
        return None
    return re.compile(r"(?<![a-z0-9_])" + re.escape(term.strip().lower())
                      + r"(?:e?s)?(?![a-z0-9_])")


def term_present(term, text):
    pat = term_pattern(term)
    return bool(pat and pat.search(text.lower()))
