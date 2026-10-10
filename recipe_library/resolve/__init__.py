"""Deciding which Fitatu product an ingredient name means.

Split in two on purpose:

* `scoring.py` is pure. Given a query and a list of candidates it returns a
  decision and the reasoning behind it, with no network and no database. This
  is where the judgement that used to happen in a model session now lives, so
  it is the part that gets tested hardest.
* `resolver.py` wires it up: build the queries to try, call `search_food`, throw
  away everything Fitatu has not verified, score what remains, and persist the
  answer so the next recipe asking for the same name costs nothing.
"""

from .resolver import ResolveOutcome, resolve_name, resolve_recipe
from .scoring import Candidate, Decision, pick, rank, score, tokens

__all__ = [
    "Candidate",
    "Decision",
    "ResolveOutcome",
    "pick",
    "rank",
    "resolve_name",
    "resolve_recipe",
    "score",
    "tokens",
]
