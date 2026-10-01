-- PG-A: EXCLUDE USING spgist on a nondeterministic-collation column
--        silently accepts constraint-violating rows
--
-- Engine : PostgreSQL 17.11 / 18.6 / master (20devel) — all reproduce.
--          Requires an ICU build (CREATE COLLATION provider=icu).
-- Manifestation: a declared integrity constraint is NOT enforced.
--   The exclusion operator `=` is collation-aware ('a' = 'A' under nd),
--   but SP-GiST text_ops stores/prunes by raw bytes, so the constraint
--   check probing 'A' never reaches the leaf containing 'a'.
--   Byte-identical duplicates ARE still rejected, which makes the
--   failure silent and easy to miss.
--
-- Upstream: BUG #19641 reports the same incompatibility for query
--   scans (false negatives). The constraint-enforcement path
--   (check_exclusion_or_unique_constraint -> index scan) is NOT
--   covered by that report or its discussed fixes.
--   The pattern-opclass guard in index.c only lists the three btree
--   pattern opclasses; SP-GiST text_ops slips through.
--
-- Expected: the second INSERT fails with 23P01.
-- Actual  : it commits; the table ends up holding a collation-equal
--           pair that violates its own declared constraint.

CREATE COLLATION nd (
    provider = icu,
    locale = 'und-u-ks-level2',
    deterministic = false
);

-- sanity: the collation really does equate 'a' and 'A'
SELECT 'a' COLLATE nd = 'A';          -- true

CREATE TABLE excl_t (
    s text COLLATE nd,
    EXCLUDE USING spgist (s WITH =)
);

INSERT INTO excl_t VALUES ('a');      -- ok
INSERT INTO excl_t VALUES ('A');      -- !! should be 23P01, silently accepted
SELECT * FROM excl_t;                 -- ('a','A')  <- constraint violated

-- byte-identical duplicates ARE caught (partial enforcement):
INSERT INTO excl_t VALUES ('a');      -- 23P01 (correct)

-- UPDATE path slips the same way when the new value is
-- collation-equal but byte-different:
CREATE TABLE excl_t2 (
    s text COLLATE nd,
    EXCLUDE USING spgist (s WITH =)
);
INSERT INTO excl_t2 VALUES ('a');
INSERT INTO excl_t2 VALUES ('b');
UPDATE excl_t2 SET s = 'A' WHERE s = 'b';   -- !! silently accepted
SELECT * FROM excl_t2;                      -- ('a','A')

-- ... while updating to a BYTE-IDENTICAL value is caught:
CREATE TABLE excl_t3 (
    s text COLLATE nd,
    EXCLUDE USING spgist (s WITH =)
);
INSERT INTO excl_t3 VALUES ('a');
INSERT INTO excl_t3 VALUES ('b');
UPDATE excl_t3 SET s = 'a' WHERE s = 'b';   -- 23P01 (correct)

-- Control: the same constraint over btree-based access is enforced.
-- (On a build with btree_gist, GiST + texteq also rejects correctly.)
CREATE TABLE excl_ctl (
    s text COLLATE nd,
    UNIQUE (s)            -- btree unique: 'A' insert rejected correctly
);
INSERT INTO excl_ctl VALUES ('a');
INSERT INTO excl_ctl VALUES ('A');    -- 23505 (correct)

-- Impact: any table declared with EXCLUDE USING spgist over an
-- nd-collated text column cannot be trusted to hold non-conflicting
-- data; INSERT, UPDATE, and ON CONFLICT probing all miss
-- collation-equal conflicts.  Silent data-integrity violation.
