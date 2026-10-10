"""Request bodies for the writing endpoints.

Only the writes get models. The read endpoints return the document shapes the
store already produces, and wrapping those in response models would mean
maintaining a second description of the same thing for no gain -- the store is
the schema, and `schema.sql` is the constraint.

`IngredientIn.quantity` is `float | None` and that null is load-bearing: it
means the source video stated no amount. The API will not accept a recipe that
substitutes a number for it, which is why there is no default.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Unit = Literal["g", "ml", "piece", "tbsp", "tsp", "cup", "pinch"]


class IngredientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Null means the video did not say. Never a placeholder for a guess.
    quantity: float | None = Field(default=None, gt=0)
    unit: Unit | None = None
    note: str | None = None
    # A per-ingredient override of the name-keyed mapping. Normally absent.
    product_id_override: str | None = None


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(min_length=1)
    # Required, because `schema.validate_recipe` requires it. Accepting it here
    # and refusing it one layer down would turn a clear field error into an
    # opaque one.
    url: str = Field(min_length=1, pattern=r"^https://")
    channel: str | None = None
    recipe_index: int = Field(default=1, ge=1)
    extracted_from: list[Literal["title", "description", "subtitles"]] | None = None


class RecipeIn(BaseModel):
    """A recipe as `/extract-recipes` produces it."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="defaults to the path id")
    title: str = Field(min_length=1)
    title_en: str | None = None
    language: Literal["pl", "en"] | None = None
    servings: int | None = Field(default=None, gt=0)
    ingredients: list[IngredientIn] = Field(min_length=1)
    source: SourceIn
    extracted_at: str | None = None
    prep_time_minutes: int | None = Field(default=None, gt=0)
    prep_time_source: str | None = None

    def to_document(self, recipe_id: str) -> dict[str, Any]:
        """The document shape `store.recipes.save` takes."""
        data = self.model_dump(exclude_none=True)
        data["id"] = recipe_id
        # Import fields are never settable through this endpoint: they are the
        # record of what Fitatu did, and only the importer may write them.
        for field in (
            "import_status",
            "fitatu_recipe_id",
            "fitatu_account_id",
            "import_error",
            "imported_at",
        ):
            data.pop(field, None)
        return data


class ExtractionIn(BaseModel):
    """The verdict stage 2 reached on a video.

    Separate from writing a recipe, because the interesting cases are the ones
    that produce no recipe: a video whose ingredients only ever appeared as
    on-screen text, which is out of scope and must not be guessed at.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "no_recipe_found", "needs_review", "unavailable"]
    note: str | None = None


class AliasIn(BaseModel):
    """A phrase to search Fitatu with for a given ingredient name."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    query: str = Field(min_length=1)
    locale: str | None = None


class MappingIn(BaseModel):
    """A hand-recorded name-to-product mapping.

    The product must already be stored, and only verified products are ever
    stored, so this is an escape hatch for the matcher and not for the
    verification rule.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    query: str | None = None


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry_failed: bool = False
    refresh: bool = False
    dry_run: bool = Field(
        default=False,
        description="consult only what is stored; do not contact Fitatu",
    )


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolve: bool = True
    retry_failed: bool = False
    shared: bool = False
    dry_run: bool = False
    tags: list[str] | None = None
    allow_partial: bool = Field(
        default=False,
        description=(
            "import a recipe whose ingredients do not all convert. Fitatu will "
            "then under-report it, so this is off by default"
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "re-send a recipe already imported. Fitatu's create is not "
            "idempotent, so this DUPLICATES it"
        ),
    )
