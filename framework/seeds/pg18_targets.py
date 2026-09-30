"""PG18-new feature + live-vein bait templates.

Surfaces that did not exist (or were heavily rewritten) in PostgreSQL 18 and
bug shapes adjacent to still-open upstream defects:

- virtual generated columns (default VIRTUAL path in 18) — in WHERE/GROUP
  BY/window/joins, ON CONFLICT EXCLUDED.gen, RULE NEW.gen, ALTER SET EXPRESSION
- WITHOUT OVERLAPS / PERIOD temporal constraints
- NOT ENFORCED constraints (18)
- OLD/NEW in RETURNING (18)
- MERGE WHEN NOT MATCHED BY SOURCE (NMBS)
- self-join-elimination bait (SJE is 18-new code; upstream #19626 class)
- appendrel + SJE interaction shapes (LATERAL UNION ALL family)
- join-removal + one-row-subquery pullup bait (upstream #19560 family)
- memoize expression-parameter bait (Brazeal thread, unfixed on master)
- nbtree skip-scan bait, group/distinct reordering, uuidv7

Same contract as pg_targets.py: {surface, setup_sqls, query, note};
queries deterministic (defined ORDER BY, no unseeded random).
Pre-18 builds error on setup — recorded, not fatal.
"""

from __future__ import annotations

PG18_TEMPLATES: list[tuple[str, list[str], str, str]] = [
    # --------------------------------------------- virtual generated cols
    ("vgencol",
     ["CREATE TABLE g1(id INT PRIMARY KEY, a INT, gen INT GENERATED ALWAYS AS (a*2) VIRTUAL)",
      "INSERT INTO g1 VALUES (1,5),(2,7),(3,NULL),(4,0)"],
     "SELECT id, gen FROM g1 WHERE gen > 10 ORDER BY id",
     "virtual gencol in WHERE"),
    ("vgencol",
     ["CREATE TABLE g1(id INT PRIMARY KEY, a INT, gen INT GENERATED ALWAYS AS (a*2) VIRTUAL)",
      "INSERT INTO g1 VALUES (1,5),(2,7),(3,3),(4,0)"],
     "SELECT gen, count(*) FROM g1 GROUP BY gen ORDER BY gen",
     "virtual gencol GROUP BY"),
    ("vgencol",
     ["CREATE TABLE g1(id INT PRIMARY KEY, a INT, gen INT GENERATED ALWAYS AS (a*2) VIRTUAL)",
      "INSERT INTO g1 VALUES (1,5),(2,7),(3,3)"],
     "SELECT id, gen, rank() OVER (ORDER BY gen) FROM g1 ORDER BY id",
     "virtual gencol in window"),
    ("vgencol",
     ["CREATE TABLE g1(id INT PRIMARY KEY, a INT, gen INT GENERATED ALWAYS AS (a*2) VIRTUAL)",
      "CREATE TABLE g2(k INT, v TEXT)",
      "INSERT INTO g1 VALUES (1,5),(2,7)",
      "INSERT INTO g2 VALUES (10,'x'),(14,'y'),(9,'z')"],
     "SELECT g1.id, g2.v FROM g1 JOIN g2 ON g1.gen = g2.k ORDER BY g1.id",
     "virtual gencol join key"),
    ("vgencol",
     ["CREATE TABLE g1(id INT PRIMARY KEY, a INT, gen INT GENERATED ALWAYS AS (a*2) VIRTUAL)",
      "INSERT INTO g1 VALUES (1,5) ON CONFLICT (id) DO UPDATE SET a = EXCLUDED.a + 1 RETURNING gen",
      "INSERT INTO g1 VALUES (1,7) ON CONFLICT (id) DO UPDATE SET a = EXCLUDED.a + 1 RETURNING gen"],
     "SELECT id, a, gen FROM g1 ORDER BY id",
     "EXCLUDED + virtual gen expansion"),
    ("vgencol",
     ["CREATE TABLE g1(id INT PRIMARY KEY, a INT, gen INT GENERATED ALWAYS AS (a*2) VIRTUAL)",
      "INSERT INTO g1 VALUES (1,5),(2,7)",
      "ALTER TABLE g1 ALTER COLUMN gen SET EXPRESSION AS (a*3)"],
     "SELECT id, gen FROM g1 ORDER BY id",
     "ALTER SET EXPRESSION recompute"),
    # ------------------------------------------------- WITHOUT OVERLAPS
    ("overlaps",
     ["CREATE TABLE bk(id INT, r INT4RANGE, PRIMARY KEY (id, r WITHOUT OVERLAPS))",
      "INSERT INTO bk VALUES (1,'[1,5)'),(1,'[5,9)'),(2,'[0,100)')"],
     "SELECT id, r FROM bk WHERE r && '[4,6)'::int4range ORDER BY id, r",
     "temporal PK range overlap probe"),
    ("overlaps",
     ["CREATE TABLE bk(id INT, r INT4RANGE, PRIMARY KEY (id, r WITHOUT OVERLAPS))",
      "INSERT INTO bk VALUES (1,'[1,5)'),(1,'[5,9)'),(2,'[0,100)')"],
     "SELECT b1.id, b2.id FROM bk b1 JOIN bk b2 ON b1.r -|- b2.r ORDER BY b1.id, b2.id",
     "adjacent ranges self join"),
    ("overlaps",
     ["CREATE TABLE bk(id INT, r INT4RANGE, UNIQUE (id, r WITHOUT OVERLAPS))",
      "INSERT INTO bk VALUES (1,'[1,5)'),(2,'[0,100)')"],
     "SELECT count(*) FROM bk WHERE '3'::int <@ r ORDER BY 1",
     "containment on overlaps constraint"),
    # ------------------------------------------------- NOT ENFORCED
    ("not_enforced",
     ["CREATE TABLE ne(a INT, b INT, CHECK (a > b) NOT ENFORCED)",
      "INSERT INTO ne VALUES (1,5),(10,2),(0,0)"],
     "SELECT a FROM ne WHERE a > b ORDER BY a",
     "unenforced check + matching qual"),
    ("not_enforced",
     ["CREATE TABLE ne(a INT, b INT, CHECK (a > 0) NOT ENFORCED)",
      "INSERT INTO ne VALUES (-1),(5),(NULL)"],
     "SELECT count(*) FROM ne WHERE a > 0 OR a IS NULL ORDER BY 1",
     "unenforced check vs NULL"),
    # ------------------------------------------------- OLD/NEW RETURNING
    ("ret_oldnew",
     ["CREATE TABLE rn(id INT PRIMARY KEY, v INT)",
      "INSERT INTO rn VALUES (1,10),(2,20),(3,30)",
      "UPDATE rn SET v = v*10 WHERE id >= 2 RETURNING id, old.v, new.v"],
     "SELECT id, v FROM rn ORDER BY id",
     "UPDATE RETURNING old/new"),
    ("ret_oldnew",
     ["CREATE TABLE rn(id INT PRIMARY KEY, v INT)",
      "INSERT INTO rn VALUES (1,10),(2,20)",
      "DELETE FROM rn WHERE id = 1 RETURNING old.id, old.v"],
     "SELECT id FROM rn ORDER BY id",
     "DELETE RETURNING old"),
    ("ret_oldnew",
     ["CREATE TABLE rn(id INT PRIMARY KEY, v INT)",
      "INSERT INTO rn VALUES (1,10)",
      "MERGE INTO rn USING (VALUES (1,5),(2,6)) s(id,d) ON rn.id = s.id "
      "WHEN MATCHED THEN UPDATE SET v = v + s.d "
      "WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.d) "
      "RETURNING merge_action(), old.v, new.v"],
     "SELECT id, v FROM rn ORDER BY id",
     "MERGE RETURNING old/new + merge_action"),
    # ------------------------------------------------- MERGE NMBS
    ("merge_nmbs",
     ["CREATE TABLE m(id INT PRIMARY KEY, v INT)",
      "INSERT INTO m VALUES (1,1),(2,2),(3,3)",
      "MERGE INTO m USING (VALUES (1),(2)) s(id) ON m.id = s.id "
      "WHEN NOT MATCHED BY SOURCE THEN DELETE"],
     "SELECT id FROM m ORDER BY id",
     "NMBS delete"),
    ("merge_nmbs",
     ["CREATE TABLE m(id INT PRIMARY KEY, v INT)",
      "INSERT INTO m VALUES (1,1),(2,2),(3,3)",
      "MERGE INTO m USING (VALUES (1),(2)) s(id) ON m.id = s.id "
      "WHEN NOT MATCHED BY SOURCE THEN UPDATE SET v = 0"],
     "SELECT id, v FROM m ORDER BY id",
     "NMBS update"),
    ("merge_nmbs",
     ["CREATE TABLE mp(id INT, v INT) PARTITION BY RANGE (id)",
      "CREATE TABLE mp1 PARTITION OF mp FOR VALUES FROM (0) TO (10)",
      "CREATE TABLE mp2 PARTITION OF mp FOR VALUES FROM (10) TO (20)",
      "INSERT INTO mp VALUES (1,1),(15,5)",
      "MERGE INTO mp USING (VALUES (1)) s(id) ON mp.id = s.id "
      "WHEN NOT MATCHED BY SOURCE THEN DELETE"],
     "SELECT id FROM mp ORDER BY id",
     "NMBS on partitioned target"),
    # ------------------------------------------------- SJE bait
    ("sje_bait",
     ["CREATE TABLE s1(id INT PRIMARY KEY, v INT)",
      "INSERT INTO s1 VALUES (1,1),(2,2),(3,3)"],
     "SELECT count(*) FROM s1 a JOIN s1 b ON a.id = b.id "
     "JOIN s1 c ON b.id = c.id WHERE a.id < 3",
     "three-way self join on unique"),
    ("sje_bait",
     ["CREATE TABLE s1(id INT PRIMARY KEY, v INT)",
      "INSERT INTO s1 VALUES (1,1),(2,2),(3,3)"],
     "SELECT a.id FROM s1 a JOIN s1 b ON a.id = b.id "
     "WHERE a.id IN (SELECT id FROM s1) ORDER BY a.id",
     "self join + self IN subquery"),
    ("sje_bait",
     ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT)",
      "INSERT INTO t0 VALUES (1,10),(2,20)"],
     "SELECT count(*) FROM t0 "
     "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c3) s "
     "ON (s.c3 IS NOT NULL) WHERE t0.c2 IN (SELECT c2 FROM t0)",
     "#19626 verbatim shape (SJE+appendrel)"),
    ("sje_bait",
     ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT)",
      "INSERT INTO t0 VALUES (1,10),(2,20)"],
     "SELECT count(*) FROM t0 "
     "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c3 + 1) s "
     "ON (s.c3 IS NOT NULL) WHERE t0.c2 IN (SELECT c2 FROM t0)",
     "appendrel arms differ"),
    ("sje_bait",
     ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT, c4 INT)",
      "INSERT INTO t0 VALUES (1,10,7),(2,20,8)"],
     "SELECT count(*) FROM t0 "
     "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c4) s "
     "ON (s.c3 IS NOT NULL) WHERE t0.c2 IN (SELECT c2 FROM t0)",
     "appendrel arms diff cols"),
    ("sje_bait",
     ["CREATE TABLE t0(c2 INT UNIQUE, c3 INT)",
      "INSERT INTO t0 VALUES (1,10),(2,20)"],
     "SELECT count(*) FROM t0 "
     "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c3) s "
     "ON (s.c3 IS NOT NULL) WHERE t0.c2 IN (SELECT c2 FROM t0)",
     "UNIQUE not PK variant"),
    ("sje_bait",
     ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT)",
      "INSERT INTO t0 VALUES (1,10),(2,20)"],
     "SELECT s.c3 FROM t0 "
     "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT t0.c3) s "
     "ON (s.c3 IS NOT NULL) WHERE t0.c2 IN (SELECT c2 FROM t0) ORDER BY s.c3",
     "appendrel output in SELECT"),
    ("sje_bait",
     ["CREATE TABLE t0(c2 INT PRIMARY KEY, c3 INT)",
      "CREATE TABLE u0(d2 INT PRIMARY KEY, d3 INT)",
      "INSERT INTO t0 VALUES (1,10),(2,20)",
      "INSERT INTO u0 VALUES (1,10),(3,30)"],
     "SELECT count(*) FROM t0 JOIN u0 ON t0.c2 = u0.d2 "
     "INNER JOIN LATERAL (SELECT t0.c3 UNION ALL SELECT u0.d3) s "
     "ON (s.c3 IS NOT NULL) WHERE t0.c2 IN (SELECT c2 FROM t0)",
     "appendrel over two rels"),
    # --------------------------------------- join-removal + PHV bait
    ("jr_phv",
     ["CREATE TABLE items (id text, owner text)",
      "CREATE TABLE follows (item_id text, user_id text, UNIQUE (user_id, item_id))",
      "INSERT INTO items VALUES ('item1', 'alice')"],
     "WITH viewer AS (SELECT 'bob' AS id) "
     "SELECT count(*) FROM items "
     "LEFT JOIN follows ON follows.item_id = items.id AND follows.user_id = 'bob' "
     "LEFT JOIN viewer ON TRUE WHERE items.owner = viewer.id",
     "#19560 verbatim shape"),
    ("jr_phv",
     ["CREATE TABLE items (id text, owner text)",
      "CREATE TABLE follows (item_id text, user_id text, UNIQUE (user_id, item_id))",
      "INSERT INTO items VALUES ('item1', 'alice'),('item2','bob'),('item3',NULL)"],
     "WITH viewer AS (SELECT 'bob' AS id) "
     "SELECT id FROM items "
     "LEFT JOIN follows ON follows.item_id = items.id AND follows.user_id = 'bob' "
     "LEFT JOIN viewer ON TRUE WHERE items.owner = viewer.id ORDER BY id",
     "multi-row version"),
    ("jr_phv",
     ["CREATE TABLE items (id text, owner text)",
      "CREATE TABLE follows (item_id text, user_id text, UNIQUE (user_id, item_id))",
      "INSERT INTO items VALUES ('item1', 'alice'),('item2','bob')"],
     "WITH viewer AS (SELECT 'bob' AS id UNION ALL SELECT 'carol') "
     "SELECT count(*) FROM items "
     "LEFT JOIN follows ON follows.item_id = items.id AND follows.user_id = 'bob' "
     "LEFT JOIN viewer ON TRUE WHERE items.owner = viewer.id",
     "two-row viewer (blocks pullup?)"),
    ("jr_phv",
     ["CREATE TABLE items (id text, owner text)",
      "CREATE TABLE follows (item_id text, user_id text, UNIQUE (user_id, item_id))",
      "INSERT INTO items VALUES ('item1', 'alice')"],
     "SELECT count(*) FROM items "
     "LEFT JOIN follows ON follows.item_id = items.id AND follows.user_id = 'bob' "
     "LEFT JOIN (SELECT 'bob' AS id) viewer ON TRUE "
     "WHERE items.owner = viewer.id",
     "inline subquery form"),
    ("jr_phv",
     ["CREATE TABLE items (id text, owner text)",
      "CREATE TABLE follows (item_id text, user_id text, UNIQUE (user_id, item_id))",
      "INSERT INTO items VALUES ('item1', 'alice')"],
     "WITH viewer AS (SELECT 'bob' AS id) "
     "SELECT count(*) FROM items "
     "LEFT JOIN viewer ON TRUE "
     "LEFT JOIN follows ON follows.item_id = items.id AND follows.user_id = 'bob' "
     "WHERE items.owner = viewer.id",
     "join order swapped"),
    # ---------------------------------------------- memoize bait
    ("memoize_bait",
     ["CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, g%10 AS ten, "
      "g%20 AS twenty, g%100 AS hundred FROM generate_series(0,1999) g",
      "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
      "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
      "ANALYZE tenk1"],
     "SELECT sum(c) FROM (SELECT t0.unique1, "
     "(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 "
     "ON t1.unique1 = t2.hundred + t0.ten WHERE t1.twenty = t0.ten) AS c "
     "FROM tenk1 t0 WHERE t0.unique1 < 100) s",
     "Brazeal expr-param shape (small)"),
    ("memoize_bait",
     ["CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, g%10 AS ten, "
      "g%20 AS twenty, g%100 AS hundred FROM generate_series(0,1999) g",
      "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
      "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
      "ANALYZE tenk1"],
     "SELECT sum(c) FROM (SELECT t0.unique1, "
     "(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 "
     "ON t1.unique1 = t2.hundred - t0.ten WHERE t1.twenty = t0.ten) AS c "
     "FROM tenk1 t0 WHERE t0.unique1 < 100) s",
     "expr-param minus variant"),
    ("memoize_bait",
     ["CREATE TABLE tenk1 AS SELECT g AS unique1, g%2 AS two, g%10 AS ten, "
      "g%20 AS twenty, g%100 AS hundred FROM generate_series(0,1999) g",
      "CREATE INDEX tenk1_unique1 ON tenk1(unique1)",
      "CREATE INDEX tenk1_hundred ON tenk1(hundred)",
      "ANALYZE tenk1"],
     "SELECT sum(c) FROM (SELECT t0.unique1, "
     "(SELECT count(*) FROM tenk1 t2 JOIN tenk1 t1 "
     "ON t1.unique1 = t2.hundred + t0.ten WHERE t1.twenty = t0.twenty) AS c "
     "FROM tenk1 t0 WHERE t0.unique1 < 100) s",
     "param in qual differs"),
    # ------------------------------------------------- skip-scan bait
    ("skipscan",
     ["CREATE TABLE sk(a INT, b INT, v INT)",
      "INSERT INTO sk SELECT g%3, g%50, g FROM generate_series(0,4999) g",
      "CREATE INDEX sk_ab ON sk(a,b)",
      "ANALYZE sk"],
     "SELECT count(*) FROM sk WHERE b = 7",
     "non-leading col qual (skip-scan bait)"),
    ("skipscan",
     ["CREATE TABLE sk(a INT, b INT, v INT)",
      "INSERT INTO sk SELECT g%3, g%50, g FROM generate_series(0,4999) g",
      "CREATE INDEX sk_ab ON sk(a,b)",
      "ANALYZE sk"],
     "SELECT a, count(*) FROM sk WHERE b = 7 GROUP BY a ORDER BY a",
     "skip-scan + group"),
    # ------------------------------------------------- reordering bait
    ("reorder18",
     ["CREATE TABLE r1(a INT, b INT, v INT)",
      "INSERT INTO r1 VALUES (1,1,1),(1,2,2),(2,1,3),(2,2,4),(NULL,1,5)"],
     "SELECT DISTINCT b, a FROM r1 ORDER BY b, a",
     "distinct reorder candidate"),
    ("reorder18",
     ["CREATE TABLE r1(a INT, b INT, v INT)",
      "INSERT INTO r1 VALUES (1,1,1),(1,2,2),(2,1,3),(2,2,4),(NULL,1,5)"],
     "SELECT a, b, count(*) FROM r1 GROUP BY GROUPING SETS ((a),(a,b),()) ORDER BY a NULLS LAST, b NULLS LAST",
     "grouping sets + reorder"),
    # ------------------------------------------------- misc 18
    ("uuid7",
     ["CREATE TABLE u1(id UUID, v INT)",
      "INSERT INTO u1 SELECT uuidv7(), g FROM generate_series(1,5) g"],
     "SELECT count(*) FROM u1 WHERE id IS NOT NULL ORDER BY 1",
     "uuidv7 deterministic shape"),
    ("rule_gencol",
     ["CREATE TABLE t (id int PRIMARY KEY, a int, gen int GENERATED ALWAYS AS (a * 2) VIRTUAL)",
      "CREATE TABLE t_log (op text, old_gen int, new_gen int)",
      "CREATE RULE t_log AS ON UPDATE TO t DO ALSO INSERT INTO t_log VALUES ('UPD', OLD.gen, NEW.gen)",
      "INSERT INTO t (id, a) VALUES (1, 5)",
      "UPDATE t SET a = 100 WHERE id = 1"],
     "SELECT op, old_gen, new_gen FROM t_log ORDER BY op",
     "RULE NEW.gen (c6a79be area)"),
    ("rule_gencol",
     ["CREATE TABLE t (id int PRIMARY KEY, a int, gen int GENERATED ALWAYS AS (a * 2) VIRTUAL)",
      "CREATE TABLE t_log (op text, new_gen int)",
      "CREATE RULE t_log AS ON INSERT TO t DO ALSO INSERT INTO t_log VALUES ('INS', NEW.gen)",
      "INSERT INTO t (id, a) VALUES (1, 5),(2,7)"],
     "SELECT op, new_gen FROM t_log ORDER BY new_gen",
     "RULE NEW.gen on INSERT"),
]


def as_seeds() -> list[dict]:
    out = []
    for i, (surface, setup, query, note) in enumerate(PG18_TEMPLATES):
        out.append({
            "setup_sqls": list(setup),
            "query": query,
            "source": f"pg18_target:{surface}#{i}",
            "engine": "postgres",
            "tags": ["pg18", surface, note],
        })
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(as_seeds(), indent=1)[:2000])
    print("count:", len(PG18_TEMPLATES))
