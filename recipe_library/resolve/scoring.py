"""Scoring Fitatu search candidates against an ingredient name. Pure.

This module replaces a judgement call that used to be made by a model reading
`search_food` output, so it is written to fail closed. Three rules do the work:

**Only verified candidates exist.** Anything Fitatu has not verified is dropped
before scoring, not down-weighted. A low score and an unverified product are
different kinds of problem and only one of them is a matching question.

**Similarity is symmetric.** A query's tokens must be found in the candidate
(recall) *and* the candidate must not be mostly tokens the query never asked
for (precision), combined as an F1. Recall alone would match "chicken" to
"chicken and leek soup with cream"; precision alone would match it to anything
short.

**An ambiguous answer is no answer.** If the two best candidates are different
products with near-identical scores, nothing is returned. The catalogue has
many near-duplicate entries and picking the first by coin-flip is exactly how a
wrong product gets in. A blank is better than a wrong id.

Polish inflection is handled by comparing token prefixes, but narrowly: tokens
count as the same word only when they agree to within their last character and
are close in length. That matches `mielona`/`mielone` and `kurczak`/`kurczaka`
while keeping `chicken` and `chickpea` apart, which a looser prefix rule does
not.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Words that carry no identifying information in either language.
STOPWORDS = frozenset(
    {
        # Polish
        "z", "ze", "i", "w", "na", "do", "bez", "o", "a", "od", "po", "oraz",
        # English
        "the", "of", "and", "with", "for", "in", "to", "or", "a", "an",
    }
)

# Below this length a token is only ever matched exactly: three characters have
# too little information for a prefix comparison to mean anything.
MIN_PREFIX_TOKEN = 4

# How far two tokens may differ in length and still be the same word.
MAX_INFLECTION_GAP = 3

# What an inflected match is worth relative to an exact one.
INFLECTION_WEIGHT = 0.9

# Applied to a branded candidate. A tiebreak toward the generic entry, not a
# veto: plenty of correct matches in this catalogue are branded.
BRAND_PENALTY = 0.97

DEFAULT_MIN_SCORE = 0.62
DEFAULT_MIN_GAP = 0.08

TOKEN_SPLIT = re.compile(r"[^0-9a-ząćęłńóśźż]+", re.IGNORECASE)


def tokens(text: str | None) -> list[str]:
    """Split a name into comparable tokens, dropping stopwords.

    Polish diacritics are kept. They are not noise: `maka` and `mąka` are
    different words, and the catalogue spells them correctly.
    """
    if not text:
        return []
    parts = [p for p in TOKEN_SPLIT.split(text.lower()) if p]
    kept = [p for p in parts if p not in STOPWORDS]
    # A name made entirely of stopwords keeps them; dropping everything would
    # score it against nothing.
    return kept or parts


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def token_similarity(a: str, b: str) -> float:
    """How much two tokens count as the same word: 1.0, INFLECTION_WEIGHT or 0."""
    if a == b:
        return 1.0
    shorter = min(len(a), len(b))
    if shorter < MIN_PREFIX_TOKEN:
        return 0.0
    if abs(len(a) - len(b)) > MAX_INFLECTION_GAP:
        return 0.0
    # Agreeing to within the shorter token's last character is what separates an
    # inflected ending from a different word that happens to start the same way.
    if _common_prefix(a, b) >= shorter - 1:
        return INFLECTION_WEIGHT
    return 0.0


def _best(token: str, others: Iterable[str]) -> float:
    return max((token_similarity(token, other) for other in others), default=0.0)


def score(query: str | None, candidate_name: str | None, *, brand: str | None = None) -> float:
    """Similarity between a search phrase and a candidate product name, 0 to 1.

    An F1 of how much of the query the candidate covers against how much of the
    candidate the query asked for, with a small nudge toward unbranded entries.
    """
    q = tokens(query)
    c = tokens(candidate_name)
    if not q or not c:
        return 0.0

    recall = sum(_best(t, c) for t in q) / len(q)
    precision = sum(_best(t, q) for t in c) / len(c)
    if recall + precision == 0:
        return 0.0

    f1 = 2 * recall * precision / (recall + precision)
    if brand:
        f1 *= BRAND_PENALTY
    return round(f1, 4)


@dataclass(frozen=True)
class Candidate:
    """One row from `search_food`, reduced to what matching needs."""

    product_id: str
    name: str | None = None
    brand: str | None = None
    source: str | None = None
    verified: bool | None = None
    query: str | None = None
    raw: dict[str, Any] | None = None

    def scored(self, query: str | None = None) -> float:
        return score(query or self.query, self.name, brand=self.brand)

    def summary(self) -> dict[str, Any]:
        """What gets recorded when this candidate is rejected."""
        return {
            "product_id": self.product_id,
            "name": self.name,
            "brand": self.brand,
            "verified": self.verified,
            "query": self.query,
            "score": self.scored(),
        }


@dataclass
class Decision:
    """The outcome of matching, and why it came out that way."""

    candidate: Candidate | None = None
    score: float = 0.0
    reason: str = ""
    ranked: list[tuple[Candidate, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.candidate is not None

    @property
    def product_id(self) -> str | None:
        return self.candidate.product_id if self.candidate else None

    def rejected(self, limit: int = 5) -> list[dict[str, Any]]:
        """The candidates considered, for the failure record."""
        return [
            dict(cand.summary(), score=value) for cand, value in self.ranked[:limit]
        ]


def verified_only(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Drop everything Fitatu has not verified.

    `verified is True`, not truthiness: a row whose flag is missing has not been
    verified, it has merely not been reported on, and the two are the same
    answer here.
    """
    return [c for c in candidates if c.verified is True]


def rank(
    query: str | None, candidates: Iterable[Candidate]
) -> list[tuple[Candidate, float]]:
    """Candidates by descending score, deterministically.

    Ties break toward the unbranded entry and then by product id, so the same
    inputs always produce the same answer -- which matters because the answer is
    cached and reused across the whole collection.
    """
    scored = [(c, c.scored(query)) for c in candidates]
    scored.sort(key=lambda pair: (-pair[1], bool(pair[0].brand), pair[0].product_id))
    return scored


def pick(
    query: str | None,
    candidates: Iterable[Candidate],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    min_gap: float = DEFAULT_MIN_GAP,
) -> Decision:
    """Choose one verified candidate, or explain why none was chosen."""
    all_candidates = list(candidates)
    if not all_candidates:
        return Decision(reason="no candidates returned by Fitatu")

    usable = verified_only(all_candidates)
    if not usable:
        return Decision(
            reason=(
                f"none of the {len(all_candidates)} candidate(s) is verified by Fitatu"
            ),
            ranked=rank(query, all_candidates),
        )

    ranked = rank(query, usable)
    top, top_score = ranked[0]

    if top_score < min_score:
        return Decision(
            reason=(
                f"best verified candidate {top.name!r} scored {top_score:.2f}, "
                f"below the {min_score:.2f} threshold"
            ),
            ranked=ranked,
        )

    # Only a *different product* can be ambiguous. The catalogue lists the same
    # product under several names, and two rows pointing at one id agree.
    for other, other_score in ranked[1:]:
        if other.product_id == top.product_id:
            continue
        if top_score - other_score < min_gap:
            return Decision(
                reason=(
                    f"ambiguous: {top.name!r} ({top_score:.2f}) and {other.name!r} "
                    f"({other_score:.2f}) are different products scoring within "
                    f"{min_gap:.2f}"
                ),
                ranked=ranked,
            )
        break

    return Decision(
        candidate=top,
        score=top_score,
        reason=f"matched {top.name!r} at {top_score:.2f}",
        ranked=ranked,
    )
