"""Single source of truth for run modes (MANUAL §23). No dependencies.

Three scripts consume modes three ways: graph_metrics.py gates by them,
validate_report.py scopes report checks by them, round.py paces and displays
them. Each previously kept its own list, and the lists drifted; each importer
keeps a literal fallback only for the case where a script is copied out of
the tree alone.
"""

VALID_MODES = ("fresh", "incremental", "anchored", "retrospective",
               "concept", "problem")

# The modes whose §0 is a novelty verdict (recommendation + falsifier
# wording, §16.3). The §23.4-§23.6 modes define their own §0 shapes.
NOVELTY_MODES = ("fresh", "incremental", "anchored")
