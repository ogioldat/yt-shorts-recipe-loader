"""Shared fixtures. Nothing here touches the network or the real database.

`FakeFitatu` is the one worth reading. It stands in for the `fitatu-api`
client, returning search rows in the shape Fitatu actually uses -- `foodId`
rather than `id`, a `verified` boolean that may be absent, measures split
across `simpleMeasures` and `measures`. The resolver's whole job is to be
careful about those, so a fake that tidied them up would test nothing.
"""

import pytest

from recipe_library import db as db_module
from recipe_library.store import products as products_store
from recipe_library.store import recipes as recipes_store


@pytest.fixture
def conn():
    """An in-memory database with the schema applied."""
    connection = db_module.fresh(":memory:")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def db_path(tmp_path):
    """A real file, for the code paths that open their own connection."""
    path = tmp_path / "library.db"
    connection = db_module.fresh(path)
    connection.close()
    return path


@pytest.fixture
def client(db_path):
    """A TestClient wired to a throwaway database."""
    from fastapi.testclient import TestClient

    from recipe_library.api.app import create_app

    return TestClient(create_app(str(db_path)))


# --------------------------------------------------------------- builders ----


def source_for(video_id="vid1", recipe_index=1):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/shorts/{video_id}",
        "channel": "Test Channel",
        "recipe_index": recipe_index,
        "extracted_from": ["title", "description"],
    }


def make_recipe(recipe_id="test-recipe", **overrides):
    """A minimal valid recipe document.

    The second ingredient deliberately has no quantity, because a third of the
    real collection does not and the rules around that are what most of these
    tests are about. Use `make_importable_recipe` where the point is a
    successful import.
    """
    data = {
        "id": recipe_id,
        "title": "Test Recipe",
        "language": "en",
        "servings": 2,
        "ingredients": [
            {"name": "chicken breast", "quantity": 500, "unit": "g"},
            {"name": "salt", "quantity": None, "unit": None, "note": "to taste"},
        ],
        "source": source_for(),
        "extracted_at": "2026-10-01",
    }
    data.update(overrides)
    return data


def make_importable_recipe(recipe_id="importable", **overrides):
    """A recipe every ingredient of which can actually reach Fitatu.

    Every quantity is in grams against a product with a gram measure, which is
    the only combination that converts without an assumption.
    """
    return make_recipe(
        recipe_id,
        ingredients=[
            {"name": "chicken breast", "quantity": 500, "unit": "g"},
            {"name": "salt", "quantity": 5, "unit": "g"},
        ],
        **overrides,
    )


def make_video(conn, video_id="vid1", **overrides):
    meta = {
        "id": video_id,
        "title": "A Short",
        "description": "ingredients: chicken, salt",
        "channel": "Test Channel",
        "channel_id": "UC1",
        "uploader": "Test",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "shorts_url": f"https://www.youtube.com/shorts/{video_id}",
        "duration": 45,
        "upload_date": "20260101",
        "original_lang": "en",
        "caption_track": "en-orig",
        "manual_sub_langs": [],
        "has_auto_captions": True,
    }
    meta.update(overrides)
    from recipe_library.store import videos as videos_store

    videos_store.save_meta(conn, video_id, meta)
    conn.commit()
    return meta


@pytest.fixture
def video(conn):
    return make_video(conn)


@pytest.fixture
def recipe(conn, video):
    data = make_recipe()
    recipes_store.save(conn, data)
    return data


@pytest.fixture
def product(conn):
    """One verified product with a gram measure and a portion measure."""
    products_store.save(
        conn,
        product_id="111",
        display_name="Pierś z kurczaka",
        source="public",
        verified=True,
        measures=[
            {"measure_id": "1", "measure_name": "g", "weight_g": 1, "kcal": 1.1},
            {"measure_id": "39", "measure_name": "porcja", "weight_g": 100, "kcal": 110},
        ],
        default_measure_id="1",
    )
    return "111"


# ------------------------------------------------------------ fake client ----


def search_row(
    product_id,
    name,
    *,
    verified=True,
    brand=None,
    row_type="PRODUCT",
    id_key="foodId",
):
    """A row shaped the way Fitatu's search endpoint shapes them."""
    row = {id_key: product_id, "name": name, "type": row_type}
    if verified is not None:
        row["verified"] = verified
    if brand is not None:
        row["brand"] = brand
    return row


def details(product_id, *, energy=110, simple=None, measures=None, initial=None):
    """A product document in the shape `get_product_details` returns."""
    out = {"id": product_id, "energy": energy}
    out["simpleMeasures"] = (
        simple
        if simple is not None
        else [
            {"id": "1", "name": "g", "weight": 1},
            {"id": "39", "name": "porcja", "weight": 100},
        ]
    )
    if measures is not None:
        out["measures"] = measures
    if initial is not None:
        out["initialMeasure"] = initial
    return out


class FakeFitatu:
    """A stand-in for `FitatuApiClient`. Records every call it is given."""

    def __init__(self, results=None, details_by_id=None, created_id="999"):
        # {(phrase, locale): [rows]} or {phrase: [rows]} for any locale.
        self.results = results or {}
        self.details_by_id = details_by_id or {}
        self.created_id = created_id
        self.searches = []
        self.detail_calls = []
        self.created = []
        self.search_error = None
        self.create_error = None

    def search_food(self, phrase, *, locale=None, limit=5, page=1):
        self.searches.append({"phrase": phrase, "locale": locale, "limit": limit})
        if self.search_error:
            raise self.search_error
        if (phrase, locale) in self.results:
            return self.results[(phrase, locale)]
        return self.results.get(phrase, [])

    def get_product_details(self, product_id):
        self.detail_calls.append(str(product_id))
        return self.details_by_id.get(str(product_id), details(str(product_id)))

    def create_recipe(self, **kwargs):
        self.created.append(kwargs)
        if self.create_error:
            raise self.create_error
        return {"id": self.created_id}


@pytest.fixture
def fake_fitatu():
    return FakeFitatu
