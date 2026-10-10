"""Resolving ingredient names to Fitatu products, without a model in the loop.

This is the phase that runs before an import. For each ingredient name:

1. If the database already maps it, stop. That is the whole point of keying the
   mapping by name rather than by ingredient row -- a name used in thirty
   recipes costs one Fitatu lookup.
2. Otherwise search Fitatu for it, under every query phrase known for that name
   and every configured locale.
3. Throw away every candidate Fitatu has not verified.
4. Score what remains (`scoring.py`) and take the winner only if it is clearly
   the winner.
5. Store the product with all its measures, and record the mapping. Or record
   the failure, with the candidates that were rejected and why.

The catalogue is Polish and most of these recipes are not, which used to be
handled by a model supplying a Polish equivalent alongside the English name.
That equivalent is now data: `query_aliases`, seeded from the phrases that
previously worked. A new English name with no alias and no Polish cognate will
not resolve, and that is reported rather than papered over -- the recipe then
fails to import with the name listed, which is the honest outcome.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..names import normalize_name
from ..store import products as products_store
from .scoring import (
    DEFAULT_MIN_GAP,
    DEFAULT_MIN_SCORE,
    Candidate,
    Decision,
    pick,
)

# Fitatu's search rows and product documents are not consistent about these, so
# every one is tried in order rather than assumed.
ID_KEYS = ("foodId", "productId", "id")
NAME_KEYS = ("name", "productName", "title", "displayName")
BRAND_KEYS = ("brand", "producer", "brandName", "manufacturer")

DEFAULT_LOCALES: tuple[str | None, ...] = ("pl_PL", None)
DEFAULT_SEARCH_LIMIT = 10

CACHED = "cached"
RESOLVED = "resolved"
UNRESOLVED = "unresolved"


@dataclass
class ResolveOutcome:
    """What happened to one ingredient name."""

    name: str
    status: str
    product_id: str | None = None
    display_name: str | None = None
    score: float | None = None
    query: str | None = None
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.product_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "product_id": self.product_id,
            "display_name": self.display_name,
            "score": self.score,
            "query": self.query,
            "reason": self.reason,
        }


# ------------------------------------------------------------ normalising ----


def _first(row: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def to_candidate(row: dict, *, query: str | None = None) -> Candidate | None:
    """Reduce a `search_food` row to what matching needs, or None if unusable.

    `verified` is read strictly: only a boolean True counts. A row that does not
    report the flag has not been verified, and treating a missing flag as a pass
    is exactly the hole this is meant to close.
    """
    if not isinstance(row, dict):
        return None
    product_id = _first(row, ID_KEYS)
    if product_id in (None, ""):
        return None
    # Search returns recipes alongside products. A Fitatu recipe is not a
    # product and cannot be an item in another recipe.
    if str(row.get("type") or "").upper() == "RECIPE":
        return None
    verified = row.get("verified")
    return Candidate(
        product_id=str(product_id),
        name=_first(row, NAME_KEYS),
        brand=_first(row, BRAND_KEYS),
        source="user" if row.get("isUserProduct") or row.get("userProduct") else "public",
        verified=verified if isinstance(verified, bool) else None,
        query=query,
        raw=row,
    )


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _energy_per_100g(details: dict) -> float | None:
    """Kilocalories per 100 g, if the product document states them."""
    for container in (details, details.get("nutrients") or {}, details.get("nutrition") or {}):
        if not isinstance(container, dict):
            continue
        for key in ("energy", "kcal", "calories"):
            value = _as_float(container.get(key))
            if value is not None:
                return value
    return None


def measures_from_details(details: dict) -> list[dict[str, Any]]:
    """Pull the measure list out of a Fitatu product document.

    The shapes differ by sub-key, as the client's own docstring warns:
    `simpleMeasures` rows carry a weight, plain `measures` rows carry only an id
    and a name. Both are kept -- a measure with no weight is still a valid
    `measureId`, and `payload.py` refuses to convert against it rather than
    guessing, which is the behaviour we want.

    `kcal` is derived from energy per 100 g where both are known. It is recorded
    for display only; nothing converts with it.
    """
    if not isinstance(details, dict):
        return []

    energy = _energy_per_100g(details)
    out: dict[str, dict[str, Any]] = {}

    for row in details.get("simpleMeasures") or []:
        if not isinstance(row, dict):
            continue
        measure_id = _first(row, ("id", "measureId"))
        if measure_id in (None, ""):
            continue
        weight = _as_float(row.get("weight")) or _as_float(row.get("capacity"))
        out[str(measure_id)] = {
            "measure_id": str(measure_id),
            "measure_name": _first(row, ("name", "key", "unitKey")),
            "weight_g": weight,
            "kcal": round(energy * weight / 100.0, 2) if (energy and weight) else None,
        }

    for row in details.get("measures") or []:
        if not isinstance(row, dict):
            continue
        measure_id = _first(row, ("id", "measureId"))
        if measure_id in (None, "") or str(measure_id) in out:
            continue
        out[str(measure_id)] = {
            "measure_id": str(measure_id),
            "measure_name": _first(row, ("name", "key", "unitKey")),
            "weight_g": None,
            "kcal": None,
        }

    initial = details.get("initialMeasure")
    default_id = None
    if isinstance(initial, dict):
        candidate_id = _first(initial, ("id", "measureId", "key"))
        if candidate_id is not None and str(candidate_id) in out:
            default_id = str(candidate_id)
            weight = _as_float(initial.get("weight"))
            if weight and not out[default_id]["weight_g"]:
                out[default_id]["weight_g"] = weight

    ordered = sorted(out.values(), key=lambda m: _measure_sort_key(m["measure_id"]))
    if default_id:
        ordered.sort(key=lambda m: m["measure_id"] != default_id)
    return ordered


def _measure_sort_key(measure_id: str) -> tuple[int, str]:
    try:
        return (int(measure_id), "")
    except (TypeError, ValueError):
        return (10**9, str(measure_id))


# --------------------------------------------------------------- querying ----


def queries_for(conn: sqlite3.Connection, name: str) -> list[tuple[str, str | None]]:
    """The (phrase, locale) pairs to try for an ingredient name, in order.

    The name itself comes first, then any recorded alias. An alias carrying its
    own locale is tried only at that locale; the bare name is tried at each
    configured locale.
    """
    key = normalize_name(name)
    out: list[tuple[str, str | None]] = [(key, None)]
    for alias in products_store.aliases(conn, key):
        phrase = alias["query"]
        if phrase and phrase != key:
            out.append((phrase, alias.get("locale")))
    return out


def _search(
    client, phrase: str, locale: str | None, limit: int
) -> tuple[list[Candidate], str | None]:
    """Run one search. Returns (candidates, error)."""
    try:
        rows = client.search_food(phrase, locale=locale, limit=limit)
    except Exception as exc:  # the client raises its own error types
        return [], f"{type(exc).__name__}: {exc}"
    candidates = [to_candidate(row, query=phrase) for row in rows or []]
    return [c for c in candidates if c is not None], None


# -------------------------------------------------------------- resolution ----


def resolve_name(
    conn: sqlite3.Connection,
    client,
    name: str,
    *,
    locales: Iterable[str | None] = DEFAULT_LOCALES,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
    min_gap: float = DEFAULT_MIN_GAP,
    refresh: bool = False,
) -> ResolveOutcome:
    """Resolve one ingredient name, using the database first.

    `client` may be None, in which case only the cache is consulted -- which is
    what a dry run wants: it reports what would still need looking up without
    touching the network.
    """
    key = normalize_name(name)
    if not key:
        return ResolveOutcome(name=key, status=UNRESOLVED, reason="empty ingredient name")

    if not refresh:
        existing = products_store.mapping(conn, key)
        if existing:
            product = products_store.get(conn, existing["product_id"])
            return ResolveOutcome(
                name=key,
                status=CACHED,
                product_id=existing["product_id"],
                display_name=(product or {}).get("display_name"),
                score=existing.get("match_score"),
                query=existing.get("query"),
                reason=f"already mapped ({existing.get('method')})",
            )

    if client is None:
        return ResolveOutcome(
            name=key,
            status=UNRESOLVED,
            reason="not in the database and no Fitatu client available",
        )

    locales = list(locales) or [None]
    best = Decision(reason="no search was attempted")
    errors: list[str] = []
    considered: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    for phrase, alias_locale in queries_for(conn, key):
        attempt_locales = [alias_locale] if alias_locale else locales
        for locale in attempt_locales:
            if (phrase, locale) in seen:
                continue
            seen.add((phrase, locale))

            candidates, error = _search(client, phrase, locale, search_limit)
            if error:
                errors.append(f"{phrase!r} @ {locale or 'default'}: {error}")
                continue
            if not candidates:
                continue

            decision = pick(phrase, candidates, min_score=min_score, min_gap=min_gap)
            considered.extend(decision.rejected())
            if decision.ok:
                return _accept(conn, client, key, phrase, decision)
            if not best.ranked or decision.ranked:
                best = decision

    if errors and not considered:
        # Every attempt failed to reach Fitatu. That is an infrastructure
        # problem, not a verdict on the name, so it is not recorded as a
        # resolution failure that would be skipped next run.
        return ResolveOutcome(
            name=key,
            status=UNRESOLVED,
            reason="Fitatu search failed: " + "; ".join(errors),
            candidates=considered,
        )

    reason = best.reason or "no candidates returned by Fitatu"
    products_store.record_failure(conn, key, reason, candidates=considered)
    return ResolveOutcome(
        name=key, status=UNRESOLVED, reason=reason, candidates=considered
    )


def _accept(
    conn: sqlite3.Connection,
    client,
    key: str,
    phrase: str,
    decision: Decision,
) -> ResolveOutcome:
    """Store the chosen product with its measures, and map the name to it."""
    candidate = decision.candidate
    assert candidate is not None  # pick() only returns a candidate when ok

    try:
        details = client.get_product_details(candidate.product_id)
    except Exception as exc:
        details = {}
        detail_error = f"{type(exc).__name__}: {exc}"
    else:
        detail_error = None

    measures = measures_from_details(details)
    if not measures:
        # Without a measure there is no way to send a quantity to Fitatu, and
        # guessing one would corrupt the nutrition data. Treat it as unresolved
        # rather than storing a product that cannot be used.
        reason = (
            f"matched {candidate.name!r} but Fitatu returned no usable measures"
            + (f" ({detail_error})" if detail_error else "")
        )
        products_store.record_failure(conn, key, reason, candidates=decision.rejected())
        return ResolveOutcome(
            name=key, status=UNRESOLVED, reason=reason, candidates=decision.rejected()
        )

    products_store.save(
        conn,
        product_id=candidate.product_id,
        display_name=candidate.name or _first(details, NAME_KEYS),
        brand=candidate.brand,
        source=candidate.source,
        verified=True,
        measures=measures,
    )
    products_store.map_name(
        conn,
        key,
        candidate.product_id,
        query=phrase,
        match_score=decision.score,
        method="auto",
    )
    return ResolveOutcome(
        name=key,
        status=RESOLVED,
        product_id=candidate.product_id,
        display_name=candidate.name,
        score=decision.score,
        query=phrase,
        reason=decision.reason,
    )


def resolve_recipe(
    conn: sqlite3.Connection,
    client,
    recipe_id: str,
    *,
    locales: Iterable[str | None] = DEFAULT_LOCALES,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
    min_gap: float = DEFAULT_MIN_GAP,
    retry_failed: bool = False,
    refresh: bool = False,
) -> list[ResolveOutcome]:
    """Resolve every ingredient name in one recipe, in order.

    Names already recorded as having no acceptable match are skipped unless
    `retry_failed`: re-running the same search against the same catalogue to get
    the same rejection wastes requests against an API that rate-limits.
    """
    rows = conn.execute(
        "SELECT DISTINCT normalized_name AS name FROM ingredients "
        "WHERE recipe_id = ? ORDER BY normalized_name",
        (recipe_id,),
    ).fetchall()

    outcomes = []
    for row in rows:
        name = row["name"]
        if not refresh and not retry_failed:
            known_failure = products_store.failure(conn, name)
            if known_failure and not products_store.mapping(conn, name):
                outcomes.append(
                    ResolveOutcome(
                        name=name,
                        status=UNRESOLVED,
                        reason=(
                            f"previously unresolved ({known_failure['reason']}); "
                            "pass retry_failed to search again"
                        ),
                        candidates=known_failure.get("candidates") or [],
                    )
                )
                continue
        outcomes.append(
            resolve_name(
                conn,
                client,
                name,
                locales=locales,
                search_limit=search_limit,
                min_score=min_score,
                min_gap=min_gap,
                refresh=refresh,
            )
        )
    return outcomes


def options_from_config(config: dict | None = None) -> dict[str, Any]:
    """Resolution settings from config.yaml, with the defaults filled in."""
    config = config or {}
    locales = config.get("search_locales")
    if not locales:
        locales = list(DEFAULT_LOCALES)
    return {
        "locales": [loc if loc else None for loc in locales],
        "search_limit": int(config.get("search_limit") or DEFAULT_SEARCH_LIMIT),
        "min_score": float(config.get("match_min_score") or DEFAULT_MIN_SCORE),
        "min_gap": float(config.get("match_min_gap") or DEFAULT_MIN_GAP),
    }
