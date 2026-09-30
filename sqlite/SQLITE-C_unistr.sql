-- SQLITE-C: unistr() does not combine surrogate pairs and emits
--           malformed UTF-8 for invalid scalar values
--
-- Engine : SQLite 3.51.2 (deterministic 10/10 fresh connections;
--          standalone verifier: sqlite_unistr_hunt.py)
-- Manifestation: wrong result + invalid UTF-8. Internal oracle - no
--   cross-engine adjudication needed: SQLite's OWN decoder treats
--   unistr's output as invalid.
--   SQLite docs state unistr "is intended to work the same as in
--   PostgreSQL, SQL Server, and Oracle"; surrogate-pair combination is
--   the feature's purpose. PostgreSQL unistr(E'😀') -> 😀
--   and rejects lone surrogates / out-of-range scalars.
-- Root cause hypothesis: the escape parser validates syntax only,
--   never scalar-value semantics - no surrogate pairing, no range check.

-- Headline: surrogate pair must combine to U+1F600 (UTF-8 F09F9880)
SELECT hex(unistr('😀'));          -- EDA0BDEDB880  (CESU-8: two raw
--                                          3-byte surrogate encodings)
SELECT hex(unistr('\U0001F600'));        -- F09F9880  (correct UTF-8)
SELECT hex(char(128512));                -- F09F9880  (correct)

-- So the pair form != the \U form for the same code point:
SELECT unistr('😀') = unistr('\U0001F600');  -- 0 (should be 1)

-- Internal oracle: SQLite's own decoder rejects unistr's output
SELECT unicode(unistr('😀'));     -- 65533  (U+FFFD)
SELECT length(unistr('😀'));     -- 2      (should be 1)

-- Lone surrogates encode raw instead of erroring:
SELECT hex(unistr('\ud83d'));                -- EDA0BD
SELECT hex(unistr('\ude00'));                -- EDB080

-- Out-of-range scalars emit malformed UTF-8 where char() sanitizes:
SELECT hex(unistr('\U00110000'));            -- F4908080 (invalid)
SELECT hex(char(1114112));                   -- EFBFBD   (FFFD)
SELECT hex(unistr('\UFFFFFFFF'));            -- longer invalid seq

-- Sanity baselines that keep working (must not regress):
SELECT hex(unistr('abc'));                   -- 616263
SELECT hex(unistr('aA'));              -- 6141
SELECT hex(unistr('a\\b'));                  -- 615C62
SELECT hex(unistr('😀'));         -- F09F9880
