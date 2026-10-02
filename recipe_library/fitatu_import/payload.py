"""Turning a recipe YAML into `create_recipe` arguments. Pure, no I/O.

Fitatu does not take grams. Each recipe item is `{itemId, measureId,
measureQuantity}`, where the quantity counts *units of the chosen measure*. So a
500 g ingredient whose mapped measure is a 150 g portion becomes 3.333 portions.
Grams-per-measure comes from the product cache, recorded when the product was
resolved.

Two rules shape everything here, and both exist to protect the nutrition data:

**No guessed conversions.** `1 piece` or `2 tbsp` cannot become grams without
knowing what one onion weighs, and this module will not invent it.

**A recipe imports whole or not at all.** A recipe landing in Fitatu with three
of its eight ingredients would silently under-report calories, which is the one
failure nobody would ever notice. Partial imports need an explicit opt-in.
"""

from dataclasses import dataclass, field

# Units that describe a count or a spoonful, not a mass. Converting them needs a
# density or a per-item weight that the source video never stated.
UNCONVERTIBLE_UNITS = ("piece", "tbsp", "tsp", "cup", "pinch")

ML_MEASURE_NAMES = ("ml", "mililitr", "millilitre", "milliliter")

QUANTITY_DECIMALS = 3


@dataclass
class Problem:
    """One ingredient that cannot be sent to Fitatu, and why."""

    ingredient: str
    reason: str

    def __str__(self) -> str:
        return f"{self.ingredient}: {self.reason}"


@dataclass
class BuildResult:
    """Either arguments ready for `create_recipe`, or the reasons there are none."""

    kwargs: dict | None = None
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kwargs is not None


def _is_ml_measure(measure: dict) -> bool:
    """Whether one unit of this measure is a millilitre."""
    return any(
        (measure.get(key) or "").strip().lower() in ML_MEASURE_NAMES
        for key in ("unit", "measure_name")
    )


def measure_quantity(
    quantity: float | None, unit: str | None, measure: dict
) -> tuple[float | None, str | None]:
    """Convert our (quantity, unit) into units of a Fitatu measure.

    Returns `(value, None)` or `(None, reason)`. Never guesses: a count or a
    spoonful has no mass, and inventing one would corrupt the very numbers this
    library exists to produce.
    """
    if quantity is None:
        return None, "no quantity stated in the source video"
    if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
        return None, f"quantity is not a number: {quantity!r}"
    if quantity <= 0:
        return None, f"quantity must be positive, got {quantity!r}"
    if not measure.get("measure_id"):
        return None, "mapped Fitatu product has no measure id"

    if unit == "ml":
        if _is_ml_measure(measure):
            return round(float(quantity), QUANTITY_DECIMALS), None
        return None, "volume in ml, but the Fitatu measure is not millilitre-based"

    if unit == "g":
        weight_g = measure.get("weight_g")
        if not weight_g or weight_g <= 0:
            return None, "Fitatu measure has no gram weight, so grams cannot be converted"
        value = round(float(quantity) / float(weight_g), QUANTITY_DECIMALS)
        if value <= 0:
            return None, f"{quantity} g rounds to zero units of a {weight_g} g measure"
        return value, None

    if unit in UNCONVERTIBLE_UNITS:
        return None, f"unit {unit!r} has no mass; Fitatu needs a weight or a volume"
    if unit is None:
        return None, "no unit stated in the source video"
    return None, f"unknown unit {unit!r}"


def _description(recipe: dict) -> str:
    """What to show in Fitatu's description field.

    This library deliberately stores no preparation steps, so the field carries
    provenance instead: the video the recipe came from. That makes a Fitatu
    recipe traceable back to its source.
    """
    source = recipe.get("source") or {}
    url = source.get("url")
    channel = source.get("channel")
    parts = [p for p in (channel, url) if p]
    return " — ".join(parts) if parts else "Imported from recipe-library"


def build_recipe_kwargs(
    recipe: dict, products: dict, *, shared: bool = False, allow_partial: bool = False
) -> BuildResult:
    """Build `create_recipe` keyword arguments from a recipe and the product cache.

    `products` is the `products` mapping of `state/fitatu_products.yaml`, keyed by
    normalised ingredient name.
    """
    from ..products import normalize_name

    problems: list[Problem] = []
    warnings: list[str] = []

    name = (recipe.get("title") or "").strip()
    if not name:
        problems.append(Problem("<recipe>", "recipe has no title"))

    items = []
    for ing in recipe.get("ingredients") or []:
        if not isinstance(ing, dict) or not ing.get("name"):
            continue
        label = ing["name"]

        entry = products.get(normalize_name(label))
        if entry is None:
            problems.append(Problem(label, "no Fitatu product mapped"))
            continue

        product_id = entry.get("fitatu_product_id")
        if not product_id:
            problems.append(Problem(label, "cache entry has no product id"))
            continue

        measure = entry.get("default_measure") or {}
        value, reason = measure_quantity(ing.get("quantity"), ing.get("unit"), measure)
        if reason is not None:
            problems.append(Problem(label, reason))
            continue

        items.append(
            {
                "type": "PRODUCT",
                "itemId": str(product_id),
                "measureId": str(measure["measure_id"]),
                "measureQuantity": value,
            }
        )

    if not items:
        problems.append(Problem("<recipe>", "no ingredients could be converted"))

    servings = recipe.get("servings")
    if not isinstance(servings, int) or isinstance(servings, bool) or servings < 1:
        # Fitatu wants a positive count. Treating the whole recipe as one portion
        # keeps the total nutrition correct; only the per-portion split changes,
        # and nothing is invented.
        warnings.append("servings not stated; importing the recipe as a single portion")
        servings = 1

    fatal = [p for p in problems if p.ingredient == "<recipe>"]
    if fatal or (problems and not allow_partial):
        return BuildResult(None, problems, warnings)

    if problems:
        warnings.append(
            f"partial import: {len(problems)} ingredient(s) omitted "
            "(--allow-partial), so Fitatu will under-report this recipe"
        )

    return BuildResult(
        {
            "name": name,
            "items": items,
            "serving": str(servings),
            "shared": shared,
            "recipe_description": _description(recipe),
        },
        problems,
        warnings,
    )
