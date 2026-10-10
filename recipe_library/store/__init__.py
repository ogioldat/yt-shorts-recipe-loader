"""Reading and writing the library. Plain SQL, one module per subject.

These modules hand back the same document shapes the file-based version used:
`videos.meta()` returns what `raw/<id>/meta.json` held, `recipes.get()` returns
what a `recipes/*.yaml` file held, and `products.cache()` returns what the
`products` mapping of `fitatu_products.yaml` held.

That is deliberate. The pure logic -- schema validation, the measure
conversions in `fitatu_import/payload.py`, the completeness rules -- was written
against those shapes and tested against them. Keeping them as the boundary
means the storage change does not reach into the parts where the judgement
lives.
"""

from . import products, recipes, videos

__all__ = ["products", "recipes", "videos"]
