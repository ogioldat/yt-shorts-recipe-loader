"""Turning a recipe YAML into `create_recipe` arguments. Pure, no I/O.

Fitatu does not take grams. Each recipe item is `{itemId, measureId,
measureQuantity}`, where the quantity counts *units of the chosen measure*. So a
500 g ingredient whose mapped measure is a 150 g portion becomes 3.333 portions.
Grams-per-measure comes from the product cache, recorded when the product was
resolved.

Two rules shape everything here, and both exist to protect the nutrition data:

**No guessed conversions.** `1 piece` or `2 tbsp` cannot become grams without
knowing what one onion weighs, and this module will not invent it. What it does
do is carry a quantity across to a measure that means the same thing: a count
into a per-item measure (piece, slice, clove), and a spoon or cup into another
spoon or cup by the definition of those volumes. Both are exact, and the weight
per unit still comes from the product, so no density is ever assumed.

**A recipe imports whole or not at all.** A recipe landing in Fitatu with three
of its eight ingredients would silently under-report calories, which is the one
failure nobody would ever notice. Partial imports need an explicit opt-in.
"""

from dataclasses import dataclass, field

# Units that describe a count or a spoonful, not a mass. Converting them needs a
# density or a per-item weight that the source video never stated.
UNCONVERTIBLE_UNITS = ("piece", "tbsp", "tsp", "cup", "pinch")

ML_MEASURE_NAMES = ("ml", "mililitr", "millilitre", "milliliter")

# Fitatu's measure ids. The *names* are localised -- the same measure comes back
# as "glass" or "szklanka" depending on the request -- so everything here keys on
# the id, which is stable.
MEASURE_GRAM = "1"
MEASURE_PINCH = "14"
MEASURE_ML = "17"

# Measures where one unit is one item of the ingredient, so a count maps across
# unchanged: `2 piece` of a product measured in slices is 2 slices. A cache entry
# may also opt in with `per_item: true`, for a `portion` or `package` that a human
# checked really is one muffin, one tortilla, one half-baguette.
PER_ITEM_MEASURE_IDS = {
    "46",  # piece / sztuka
    "8",  # slice / plaster
    "25",  # clove / ząbek
    "5",  # floret / różyczka
    "13",  # slice of bread / kromka
    "182",  # cube / kostka
}

# A spoon or a cup only converts into *the same* spoon or cup. Scaling between
# different ones looks exact on paper but is not: Fitatu's per-spoon weights are
# kitchen figures, not volumetric. Kotányi granulated garlic lists a teaspoon at
# 10 g and a tablespoon at 20 g -- a ratio of 2, where the volumes say 3. So
# multiplying one into the other would inject up to 50% error, and resolving a
# product that carries the measure the recipe actually uses is the fix instead.
#
# There are two exceptions, both weight-free:
#
#   * a 240 ml cup against a Polish 250 ml `szklanka`, a 4% gap between measures
#     that really are the same kind of thing;
#   * anything with a volume against a **millilitre** measure. A tablespoon is
#     15 ml by definition, and a millilitre measure needs no spoon convention at
#     all, so this adds no assumption. It is also the route for a recipe written
#     in ml against a product that only offers a glass.
UNIT_MEASURE_IDS = {
    "tsp": {"19", "11"},  # teaspoon / łyżeczka, level teaspoon
    "tbsp": {"20"},  # tablespoon / łyżka
    "cup": {"32"},  # glass / szklanka
    "ml": {MEASURE_ML},
}
CUP_TO_GLASS = 240.0 / 250.0

# Millilitres in one unit, for the volume family only.
UNIT_ML = {"ml": 1.0, "tsp": 5.0, "tbsp": 15.0, "cup": 240.0}
MEASURE_UNIT_ML = {MEASURE_ML: 1.0, "19": 5.0, "11": 5.0, "20": 15.0, "32": 250.0}

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
    if str(measure.get("measure_id") or "").strip() == MEASURE_ML:
        return True
    return any(
        (measure.get(key) or "").strip().lower() in ML_MEASURE_NAMES
        for key in ("unit", "measure_name")
    )


def _measure_name(measure: dict) -> str:
    """A human label for error messages only; never matched on."""
    return (measure.get("measure_name") or measure.get("measure_id") or "?").strip()


def _measure_id(measure: dict) -> str:
    return str(measure.get("measure_id") or "").strip()


def _is_per_item_measure(measure: dict) -> bool:
    """Whether one unit of this measure is one item of the ingredient."""
    return bool(measure.get("per_item")) or _measure_id(measure) in PER_ITEM_MEASURE_IDS


UNIT_WANTED_MEASURE_IDS = {
    "g": {MEASURE_GRAM},
    "ml": {MEASURE_ML},
    "pinch": {MEASURE_PINCH},
    "piece": PER_ITEM_MEASURE_IDS,
    "tsp": UNIT_MEASURE_IDS["tsp"],
    "tbsp": UNIT_MEASURE_IDS["tbsp"],
    "cup": UNIT_MEASURE_IDS["cup"],
}


def select_measure(entry: dict, unit: str | None) -> dict:
    """Pick the cached measure that matches how the recipe states the amount.

    A cache entry holds every measure Fitatu offers for the product, because one
    ingredient is written in different units across recipes -- garlic powder by
    the teaspoon in one and by the tablespoon in another. Picking the matching
    measure is what makes those exact instead of a scaled guess. Falls back to
    the entry's default measure, which keeps the old behaviour.
    """
    default = entry.get("default_measure") or {}
    wanted = UNIT_WANTED_MEASURE_IDS.get(unit or "")
    if not wanted:
        return default
    if str(default.get("measure_id") or "") in wanted or (
        unit == "piece" and default.get("per_item")
    ):
        return default
    for candidate in entry.get("measures") or []:
        if str(candidate.get("measure_id") or "") in wanted and candidate.get("weight_g"):
            return candidate
    if unit in UNIT_ML:
        # No exact measure: a millilitre one converts by definition, and for a
        # recipe written in ml any volume measure does.
        preferred = [MEASURE_ML] if unit != "ml" else list(MEASURE_UNIT_ML)
        for want in preferred:
            for candidate in entry.get("measures") or []:
                if str(candidate.get("measure_id") or "") == want and candidate.get("weight_g"):
                    return candidate
    return default


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

    if unit == "g":
        weight_g = measure.get("weight_g")
        if not weight_g or weight_g <= 0:
            return None, "Fitatu measure has no gram weight, so grams cannot be converted"
        value = round(float(quantity) / float(weight_g), QUANTITY_DECIMALS)
        if value <= 0:
            return None, f"{quantity} g rounds to zero units of a {weight_g} g measure"
        return value, None

    if unit == "piece":
        if _is_per_item_measure(measure):
            return round(float(quantity), QUANTITY_DECIMALS), None
        return None, (
            f"a count of pieces, but the Fitatu measure is "
            f"{_measure_name(measure)!r}, which is not one item"
        )

    if unit in UNIT_MEASURE_IDS:
        measure_id = _measure_id(measure)
        factor = None
        if measure_id in UNIT_MEASURE_IDS[unit] or (unit == "ml" and _is_ml_measure(measure)):
            factor = CUP_TO_GLASS if unit == "cup" else 1.0
        elif _is_ml_measure(measure) or unit == "ml":
            # Volume into volume, by the definition of the units: no spoon
            # convention and no density enter into it.
            target_ml = MEASURE_UNIT_ML.get(measure_id)
            if target_ml is None and _is_ml_measure(measure):
                target_ml = 1.0
            if target_ml:
                factor = UNIT_ML[unit] / target_ml
        if factor is None:
            return None, (
                f"the recipe measures this in {unit}, but the mapped product's measure "
                f"is {_measure_name(measure)!r}; resolve a product that has a {unit} "
                f"or a millilitre measure"
            )
        value = round(float(quantity) * factor, QUANTITY_DECIMALS)
        if value <= 0:
            return None, f"{quantity} {unit} rounds to zero units of that measure"
        return value, None

    if unit == "pinch":
        if _measure_id(measure) == MEASURE_PINCH:
            return round(float(quantity), QUANTITY_DECIMALS), None
        return None, "a pinch has no defined volume, so it converts to nothing else"

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
    recipe: dict,
    products: dict,
    *,
    shared: bool = False,
    allow_partial: bool = False,
    tags: list[str] | None = None,
) -> BuildResult:
    """Build `create_recipe` keyword arguments from a recipe and the product cache.

    `products` is the `products` mapping of `state/fitatu_products.yaml`, keyed by
    normalised ingredient name.
    """
    from ..names import normalize_name

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

        measure = select_measure(entry, ing.get("unit"))
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

    kwargs = {
        "name": name,
        "items": items,
        "serving": str(servings),
        "shared": shared,
        "recipe_description": _description(recipe),
    }

    # Fitatu's `tags` field only accepts values from its own fixed vocabulary
    # (cuisines, diet types). Posting a custom one makes the API answer 500, so
    # the tag goes into the description, which is free text and searchable.
    if tags:
        kwargs["recipe_description"] = " — ".join(
            [kwargs["recipe_description"], " ".join(f"#{t}" for t in tags)]
        )

    minutes = recipe.get("prep_time_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0:
        kwargs["cooking_time"] = minutes
    elif minutes is not None:
        warnings.append(f"ignoring prep_time_minutes: {minutes!r} is not a positive integer")

    return BuildResult(kwargs, problems, warnings)
