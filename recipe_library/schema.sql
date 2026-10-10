-- The recipe library, as one SQLite database.
--
-- Everything that used to be a file lives here: the fetch ledger (was
-- state/processed.yaml), the raw video text (was raw/<id>/meta.json and
-- subs.<lang>.txt), the recipes (was recipes/*.yaml) and the Fitatu product
-- cache (was state/fitatu_products.yaml).
--
-- Two invariants are expressed as constraints rather than as conventions,
-- because a convention is something a future call site forgets:
--
--   * `products.verified` is CHECKed to 1. An unverified Fitatu product cannot
--     be stored at all, so "only verified products" cannot be bypassed by a
--     code path that neglects to filter.
--   * a recipe with `import_status = 'ok'` must carry a `fitatu_recipe_id`.
--     Fitatu's POST /recipes is not idempotent, and that id is the only
--     evidence distinguishing an imported recipe from a duplicate waiting to
--     happen.
--
-- Quantities stay nullable throughout. A null quantity means the source video
-- never stated an amount; it is never a placeholder for a guess.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

-- ---------------------------------------------------------------- videos ----

-- The ledger. One row per playlist video, fetched or not.
--
-- `description_sha256` gates re-extraction and nothing else. Fetching is gated
-- on what is actually stored for a video, because the playlist listing returns
-- ids alone and the description is unknown at that point. Conflating the two
-- was a real bug in an early draft; see tests/test_ledger.py.
CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    description_sha256  TEXT,
    channel             TEXT,
    channel_id          TEXT,
    uploader            TEXT,
    webpage_url         TEXT,
    shorts_url          TEXT,
    duration            INTEGER,
    upload_date         TEXT,
    -- Which caption track is the real speech recognition output. YouTube lists
    -- auto-captions in ~157 languages but exactly one is not a machine
    -- translation of another; see completeness.py.
    original_lang       TEXT,
    caption_track       TEXT,
    manual_sub_langs    TEXT,  -- JSON array
    has_auto_captions   INTEGER NOT NULL DEFAULT 0,
    -- Two statuses, not one. `state/processed.yaml` had a single `status` that
    -- fetch wrote ('complete', 'partial') and extraction then overwrote ('ok',
    -- 'needs_review', 'no_recipe_found'), so the two stages kept erasing each
    -- other's answer and neither could be queried. They are separate gates --
    -- fetching is gated on what is stored, extraction on the description hash
    -- -- so they get separate columns.
    fetch_status        TEXT NOT NULL DEFAULT 'pending'
                        CHECK (fetch_status IN
                            ('pending', 'partial', 'complete', 'unavailable')),
    extract_status      TEXT CHECK (extract_status IS NULL OR extract_status IN
                            ('ok', 'no_recipe_found', 'needs_review', 'unavailable')),
    note                TEXT,
    fetched_at          TEXT,
    extracted_at        TEXT
);

CREATE INDEX IF NOT EXISTS videos_fetch_status ON videos (fetch_status);
CREATE INDEX IF NOT EXISTS videos_extract_status ON videos (extract_status);

-- The subtitle text, one row per language.
--
-- Plain text only. Auto-caption VTT is a rolling window of repeated lines with
-- a timestamp on every word, and the timings are used nowhere, so subtitles.py
-- collapses it to prose before it reaches this table.
CREATE TABLE IF NOT EXISTS video_subtitles (
    video_id    TEXT NOT NULL REFERENCES videos (video_id) ON DELETE CASCADE,
    lang        TEXT NOT NULL,
    track       TEXT,  -- the yt-dlp key it came from, e.g. pl-orig
    content     TEXT NOT NULL,
    fetched_at  TEXT,
    PRIMARY KEY (video_id, lang)
);

-- -------------------------------------------------------------- products ----

-- Fitatu products. Verified only, by constraint.
CREATE TABLE IF NOT EXISTS products (
    product_id      TEXT PRIMARY KEY,
    display_name    TEXT,
    brand           TEXT,
    source          TEXT CHECK (source IS NULL OR source IN ('user', 'public')),
    verified        INTEGER NOT NULL DEFAULT 1 CHECK (verified = 1),
    first_seen_at   TEXT
);

-- Every measure Fitatu offers for a product.
--
-- Fitatu takes measures, not grams: an item is {itemId, measureId,
-- measureQuantity} counting units of that measure. One ingredient is written in
-- different units across recipes -- garlic powder by the teaspoon in one and the
-- tablespoon in another -- so keeping all of them is what makes the conversion
-- exact instead of a scaled guess.
CREATE TABLE IF NOT EXISTS product_measures (
    product_id      TEXT NOT NULL REFERENCES products (product_id) ON DELETE CASCADE,
    measure_id      TEXT NOT NULL,
    measure_name    TEXT,
    weight_g        REAL,
    kcal            REAL,
    -- One unit of this measure is one item of the ingredient, so a count maps
    -- across unchanged. Set by a human for a `portion` that really is one
    -- muffin; see fitatu_import/payload.py.
    per_item        INTEGER NOT NULL DEFAULT 0,
    is_default      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, measure_id)
);

-- The resolve-once cache: a normalised ingredient name to the product it means.
--
-- Keyed by name rather than by ingredient row, so an ingredient appearing in
-- thirty recipes costs one Fitatu lookup. `query` records the phrase that
-- actually found the product, which is not always the ingredient name: the
-- catalogue is Polish, so an English ingredient is often found by its Polish
-- equivalent. Keeping it makes the match auditable.
CREATE TABLE IF NOT EXISTS ingredient_products (
    name            TEXT PRIMARY KEY,  -- normalised ingredient name
    product_id      TEXT NOT NULL REFERENCES products (product_id) ON DELETE RESTRICT,
    query           TEXT,
    match_score     REAL,
    method          TEXT NOT NULL DEFAULT 'auto'
                    CHECK (method IN ('seeded', 'auto', 'manual')),
    resolved_at     TEXT
);

-- Names with no acceptable verified match.
--
-- A blank is better than a wrong id, so an unresolved name is a normal outcome
-- and is recorded rather than retried blindly. `candidates` keeps what was
-- considered and why it was rejected, so a threshold change is reviewable.
CREATE TABLE IF NOT EXISTS ingredient_resolution_failures (
    name        TEXT PRIMARY KEY,
    reason      TEXT NOT NULL,
    candidates  TEXT,  -- JSON array of what was considered
    attempts    INTEGER NOT NULL DEFAULT 1,
    checked_at  TEXT
);

-- Phrases to send to `search_food` for a given ingredient name.
--
-- This is the bilingual dictionary that replaces a model's judgement about what
-- "self-rising flour" is called in a Polish catalogue. Seeded from the phrases
-- that previously worked, and extendable by hand when a new name does not
-- resolve.
CREATE TABLE IF NOT EXISTS query_aliases (
    name        TEXT NOT NULL,  -- normalised ingredient name
    query       TEXT NOT NULL,
    locale      TEXT,
    added_at    TEXT,
    PRIMARY KEY (name, query)
);

-- --------------------------------------------------------------- recipes ----

CREATE TABLE IF NOT EXISTS recipes (
    id                  TEXT PRIMARY KEY,  -- the slug
    title               TEXT NOT NULL,
    title_en            TEXT,
    language            TEXT CHECK (language IS NULL OR language IN ('pl', 'en')),
    servings            INTEGER CHECK (servings IS NULL OR servings > 0),
    video_id            TEXT REFERENCES videos (video_id) ON DELETE SET NULL,
    recipe_index        INTEGER NOT NULL DEFAULT 1 CHECK (recipe_index >= 1),
    source_url          TEXT,
    channel             TEXT,
    extracted_from      TEXT,  -- JSON array: title, description, subtitles
    extracted_at        TEXT,
    prep_time_minutes   INTEGER,
    prep_time_source    TEXT,
    import_status       TEXT NOT NULL DEFAULT 'not_imported'
                        CHECK (import_status IN ('ok', 'error', 'not_imported')),
    fitatu_recipe_id    TEXT,
    fitatu_account_id   TEXT,
    import_error        TEXT,
    import_note         TEXT,
    imported_at         TEXT,
    -- Fitatu's create is not idempotent, so a recipe claiming to be imported
    -- must say which Fitatu recipe it became.
    CHECK (import_status <> 'ok' OR (fitatu_recipe_id IS NOT NULL AND fitatu_recipe_id <> ''))
);

CREATE UNIQUE INDEX IF NOT EXISTS recipes_video_slot
    ON recipes (video_id, recipe_index) WHERE video_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS recipes_import_status ON recipes (import_status);
CREATE INDEX IF NOT EXISTS recipes_language ON recipes (language);

-- Earlier imports of the same recipe, under this or another Fitatu account.
--
-- Which account a recipe landed on is not cosmetic: the same library imported
-- under two accounts leaves two disjoint sets, and an id recorded under one
-- account says nothing about the other.
CREATE TABLE IF NOT EXISTS recipe_imports (
    recipe_id           TEXT NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    fitatu_recipe_id    TEXT NOT NULL,
    fitatu_account_id   TEXT,
    imported_at         TEXT,
    PRIMARY KEY (recipe_id, fitatu_recipe_id)
);

CREATE TABLE IF NOT EXISTS ingredients (
    recipe_id       TEXT NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    -- Null means the video stated no amount. Never a guess.
    quantity        REAL CHECK (quantity IS NULL OR quantity > 0),
    unit            TEXT CHECK (unit IS NULL OR unit IN
                        ('g', 'ml', 'piece', 'tbsp', 'tsp', 'cup', 'pinch')),
    note            TEXT,
    -- A per-ingredient override of the name-keyed mapping, for the rare case
    -- where one recipe means something different by a common word. Normally
    -- null, and the mapping in ingredient_products applies.
    product_id      TEXT REFERENCES products (product_id) ON DELETE SET NULL,
    PRIMARY KEY (recipe_id, position),
    CHECK (quantity IS NULL OR unit IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ingredients_name ON ingredients (normalized_name);

-- What product each ingredient resolves to.
--
-- This view is what used to be `products apply`: a separate deterministic pass
-- that copied ids into 72 YAML files and had to be re-run whenever the cache
-- grew. As a join there is nothing to run and nothing to forget.
CREATE VIEW IF NOT EXISTS ingredients_resolved AS
SELECT
    i.recipe_id,
    i.position,
    i.name,
    i.normalized_name,
    i.quantity,
    i.unit,
    i.note,
    COALESCE(i.product_id, m.product_id) AS product_id,
    i.product_id IS NOT NULL             AS is_override,
    m.query                              AS match_query,
    m.match_score                        AS match_score,
    m.method                             AS match_method
FROM ingredients i
LEFT JOIN ingredient_products m ON m.name = i.normalized_name;
