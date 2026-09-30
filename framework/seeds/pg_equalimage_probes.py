"""Equal-image probes: types where `=` groups values whose internal image
differs, crossed with every qual-pushdown grouping mechanism.

Mechanism (clauses.c expression_has_grouping_conflict, ~5496): under a
deterministic collation the walker only rejects a grouping column referenced
as a bare non-operand Var when the collation is nondeterministic.  A column
buried inside a function / cast / subscript / RowExpr therefore escapes the
check entirely ("This leaves one case uncaught ... record_image_ops over a
rebuilt record, or scale() over numeric"), so an outer qual that can tell two
eq-equal members apart is pushed below the grouping and silently drops member
rows.  Same admitted gap the #19649 patch left open — these are the uncovered
classes, systematically expanded.

Type x mechanism matrix (all eq-equal pairs whose images differ):

  numeric  1.0 vs 1.00      discriminated by scale(n), n::text
  interval '1 mon' vs '30 days'   by date '2020-01-31'+i  (calendar-aware:
                            Jan31+1mon = Feb29 leap; +30days = Mar1)
  interval '1 day' vs 24:00:00    by i::text
  float8   0 vs -0          by f::text  ('0' vs '-0'; 1/f is NOT usable —
                            PG raises division-by-zero, not IEEE inf)
  bpchar   'a'/'a '/'a  '   by octet_length(b)  (b::text rtrims -> useless)
  record   row(n,..) image  by pg_catalog.*=  (record_image_eq; single-field
                            ROW() degenerates to the scalar, and anonymous
                            ROW() also fails opr lookup -> needs a named
                            composite cast)
  numeric[] {1.0}/{1.00}    by scale(a[1]), a::text
  numrange [1.0,3)/[1.00,3) by scale(lower(r))
  domain-over-numeric      by scale(n)  (domain eq is base-type eq)

Grouping mechanisms exercised: outer-WHERE over GROUP BY subquery,
HAVING->WHERE pushdown, window PARTITION BY, plain DISTINCT, DISTINCT ON
(rep pinned by secondary ORDER BY), UNION/INTERSECT dedup, GROUPING SETS.

Oracle determinism / rep-arbitrariness
--------------------------------------
A GROUP BY emits ONE row per eq-class but the group key it prints is an
arbitrary member ("rep").  Empirically the first-inserted member wins
(seqscan -> hashagg / setop-dedup keeps first-seen).  Two consequences:

  * Window PARTITION BY arms are rep-free oracles: every member row is
    emitted with the full partition count, so the outer qual selects the
    member deterministically.  These are the primary witnesses.
  * GROUP BY / HAVING / dedup arms are written so the first-inserted member
    decides: arms ending _rep expect the full count, arms ending _nonrep
    expect zero rows.  A rep flip on a clean engine swaps (full-count <-> 0
    rows) — that is legal nondeterminism, NOT a hit.  The buggy signature is
    specifically a surviving row whose count is 1 (group shrunk by the
    pushed qual) or a phantom row a dedup cannot emit.

Normalizer note: numeric 1.0/1.00 and float 0.0/-0.0 normalize identically,
so rep leakage through numeric/float output columns is invisible anyway;
only row existence and aggregate values are adjudicated.
"""

T_J = [
    "CREATE TABLE t(id int primary key, j jsonb)",
    "INSERT INTO t VALUES (1,'1'),(2,'1.0')",
]
T_NUM = [
    "CREATE TABLE tn(id int primary key, n numeric)",
    "INSERT INTO tn VALUES (1,1.0),(2,1.00)",
]
T_NUM_REC = T_NUM + [
    "CREATE TYPE nrec AS (v numeric, tag int)",
]
T_INT = [
    "CREATE TABLE ti(id int primary key, i interval)",
    # '1 mon' first: it is the rep AND it is the member whose calendar
    # addition lands on 2020-02-29.
    "INSERT INTO ti VALUES (1,'1 mon'),(2,'30 days')",
]
T_INT2 = [
    "CREATE TABLE ti2(id int primary key, i interval)",
    "INSERT INTO ti2 VALUES (1,'1 day'::interval),"
    "(2,make_interval(hours=>24))",
]
T_FLT = [
    "CREATE TABLE tf(id int primary key, f float8)",
    # '-0'::float8 literal required: numeric literal -0.0 has no neg zero.
    "INSERT INTO tf VALUES (1,'0'::float8),(2,'-0'::float8)",
]
T_BP = [
    "CREATE TABLE tb(id int primary key, b bpchar)",
    # all bpchar-equal; octet_length 1/2/3 distinguishes the images.
    "INSERT INTO tb VALUES (1,'a'),(2,'a '),(3,'a  ')",
]
T_ARR = [
    "CREATE TABLE ta(id int primary key, a numeric[])",
    "INSERT INTO ta VALUES (1,'{1.0}'::numeric[]),(2,'{1.00}'::numeric[])",
]
T_RNG = [
    "CREATE TABLE tr(id int primary key, r numrange)",
    "INSERT INTO tr VALUES (1,numrange(1.0,3)),(2,numrange(1.00,3))",
]
T_DOM = [
    "CREATE DOMAIN dnum AS numeric",
    "CREATE TABLE td(id int primary key, n dnum)",
    "INSERT INTO td VALUES (1,1.0),(2,1.00)",
]

_AFFECTED_ALL = {15: (0, 19), 16: (0, 15), 17: (0, 11), 18: (0, 6),
                 20: (0, 0)}


def _case(name, source, setup, query, expected, marker,
          buggy="wrong_rows", buggy_error=None):
    d = {
        "name": name,
        "source": source,
        "setup_sqls": setup,
        "pre_sqls": [],
        "query": query,
        "buggy": buggy,
        "buggy_marker": marker,
        "affected": _AFFECTED_ALL,
    }
    if expected is not None:
        d["expected_rows"] = expected
    if buggy_error is not None:
        d["buggy_error"] = buggy_error
    return d


PROBES = [
    # ------------------------------------------------ firing control
    _case("eq_jsonb_verbatim_ctl",
          "#19649 verbatim control — known live everywhere.",
          T_J,
          "SELECT j, c FROM (SELECT j, count(*) c FROM t GROUP BY j) s "
          "WHERE j::text = '1'",
          [[1, 2]], "pushed => (1,1)"),

    # ---------------- P1: numeric x window / GROUP BY / HAVING / GSETS
    _case("eq_num_win_scale",
          "P1 verbatim: numeric x window PARTITION BY; rep-free oracle "
          "(window emits both member rows, qual selects scale=1 member).",
          T_NUM,
          "SELECT n, c FROM (SELECT n, count(*) OVER (PARTITION BY n) c "
          "FROM tn) s WHERE scale(n)=1",
          [[1.0, 2]], "pushed below window partition => (1.0,1)"),
    _case("eq_num_gb_scale_rep",
          "numeric x GROUP BY, qual matches first-inserted rep member.",
          T_NUM,
          "SELECT n, c FROM (SELECT n, count(*) c FROM tn GROUP BY n) s "
          "WHERE scale(n)=1",
          [[1.0, 2]], "pushed => (1.0,1); rep flip to 1.00 => empty (legal)"),
    _case("eq_num_gb_scale_nonrep",
          "numeric x GROUP BY, qual matches only the non-rep member: "
          "correct = empty (rep '1.0' fails); pushed keeps {1.00} => 1 row.",
          T_NUM,
          "SELECT c FROM (SELECT n, count(*) c FROM tn GROUP BY n) s "
          "WHERE scale(n)=2",
          [], "pushed => (1); rep flip => (2) (legal)"),
    _case("eq_num_hav_scale",
          "numeric x HAVING->WHERE conversion.",
          T_NUM,
          "SELECT count(*) c FROM tn GROUP BY n HAVING scale(n)=1",
          [[2]], "HAVING pushed to WHERE => 1; rep flip => 0 rows (legal)"),
    _case("eq_num_gsets_scale",
          "numeric x GROUPING SETS ((n),()): pushed only into the (n) path; "
          "sum(c) 2 vs 1.",
          T_NUM,
          "SELECT sum(c) FROM (SELECT n, count(*) c FROM tn "
          "GROUP BY GROUPING SETS ((n),())) s WHERE scale(n)=1",
          [[2]], "partial pushdown => 1; rep flip => NULL (legal)"),

    # ---------------- P2: interval x calendar arithmetic / ::text
    _case("eq_int_hav_dateadd",
          "P2 verbatim: '1 mon' = '30 days' under interval_eq but "
          "date '2020-01-31'+i lands Feb29 only for '1 mon'.",
          T_INT,
          "SELECT count(*) c FROM ti GROUP BY i "
          "HAVING date '2020-01-31'+i = '2020-02-29'",
          [[2]], "HAVING pushed => 1; rep flip => 0 rows (legal)"),
    _case("eq_int_hav_dateadd_nonrep",
          "same, qual matches only '30 days': correct empty under rep "
          "'1 mon'.",
          T_INT,
          "SELECT count(*) c FROM ti GROUP BY i "
          "HAVING date '2020-01-31'+i = '2020-03-01'",
          [], "pushed => 1; rep flip => 2 (legal)"),
    _case("eq_int_win_dateadd",
          "P2 window variant — rep-free.",
          T_INT,
          "SELECT i::text, c FROM (SELECT i, count(*) OVER "
          "(PARTITION BY i) c FROM ti) s "
          "WHERE date '2020-01-31'+i = '2020-02-29'",
          [["1 mon", 2]], "pushed => ('1 mon',1)"),
    _case("eq_int_win_dateadd_nonrep",
          "P2 window variant on the '30 days' member — rep-free.",
          T_INT,
          "SELECT i::text, c FROM (SELECT i, count(*) OVER "
          "(PARTITION BY i) c FROM ti) s "
          "WHERE date '2020-01-31'+i = '2020-03-01'",
          [["30 days", 2]], "pushed => ('30 days',1)"),
    _case("eq_int_win_text",
          "interval '1 day' vs make_interval(hours=>24): eq-equal, "
          "i::text distinguishes ('1 day'/'24:00:00').",
          T_INT2,
          "SELECT i::text, c FROM (SELECT i, count(*) OVER "
          "(PARTITION BY i) c FROM ti2) s WHERE i::text='1 day'",
          [["1 day", 2]], "pushed => ('1 day',1)"),
    _case("eq_int_gb_text",
          "interval ::text x GROUP BY.",
          T_INT2,
          "SELECT i::text, c FROM (SELECT i, count(*) c FROM ti2 "
          "GROUP BY i) s WHERE i::text='1 day'",
          [["1 day", 2]], "pushed => ('1 day',1); rep flip => empty"),

    # ---------------- P3: float8 +-0 (1/f unusable: PG raises div-by-zero)
    _case("eq_flt_win_neg0",
          "P3: float8 0/-0 eq-equal; f::text distinguishes. Rep-free "
          "window oracle; normalized -0.0 == 0.0.",
          T_FLT,
          "SELECT f, c FROM (SELECT f, count(*) OVER (PARTITION BY f) c "
          "FROM tf) s WHERE f::text='-0'",
          [[0.0, 2]], "pushed => (-0,1)"),
    _case("eq_flt_win_pos0",
          "float8 window, qual on the +0 member.",
          T_FLT,
          "SELECT f, c FROM (SELECT f, count(*) OVER (PARTITION BY f) c "
          "FROM tf) s WHERE f::text='0'",
          [[0.0, 2]], "pushed => (0,1)"),
    _case("eq_flt_gb_neg0",
          "float8 x GROUP BY, qual on non-rep '-0': correct empty.",
          T_FLT,
          "SELECT c FROM (SELECT f, count(*) c FROM tf GROUP BY f) s "
          "WHERE f::text='-0'",
          [], "pushed => (1); rep flip => (2) (legal)"),
    _case("eq_flt_hav_pos0",
          "float8 x HAVING->WHERE.",
          T_FLT,
          "SELECT count(*) c FROM tf GROUP BY f HAVING f::text='0'",
          [[2]], "pushed => 1; rep flip => 0 rows (legal)"),

    # ---------------- P5: bpchar x octet_length (only image-aware func)
    _case("eq_bp_win_octet2",
          "P5: 'a'/'a '/'a  ' all bpchar-equal; octet_length sees the "
          "stored trailing blanks (b::text rtrims => useless). Window arm "
          "is rep-free.",
          T_BP,
          "SELECT octet_length(b) AS l, c FROM (SELECT b, count(*) OVER "
          "(PARTITION BY b) c FROM tb) s WHERE octet_length(b)=2",
          [[2, 3]], "pushed => (2,1)"),
    _case("eq_bp_win_octet3",
          "bpchar window, octet_length=3 member.",
          T_BP,
          "SELECT octet_length(b) AS l, c FROM (SELECT b, count(*) OVER "
          "(PARTITION BY b) c FROM tb) s WHERE octet_length(b)=3",
          [[3, 3]], "pushed => (3,1)"),
    _case("eq_bp_hav_octet2",
          "P5 verbatim: GROUP BY b HAVING octet_length(b)=2; rep 'a' "
          "(len 1) fails => correct empty.",
          T_BP,
          "SELECT count(*) c FROM tb GROUP BY b HAVING octet_length(b)=2",
          [], "HAVING pushed => 1; rep flip => 3 (legal)"),
    _case("eq_bp_hav_octet1",
          "bpchar HAVING on the rep member: correct = 3.",
          T_BP,
          "SELECT count(*) c FROM tb GROUP BY b HAVING octet_length(b)=1",
          [[3]], "pushed => 1"),
    _case("eq_bp_distinct_octet3",
          "bpchar x DISTINCT dedup, qual matches only the last-inserted "
          "image: correct = 0 rows regardless of rep ('a'/'a ' both fail "
          "len 3, 'a  ' is never first-seen).",
          T_BP,
          "SELECT count(*) FROM (SELECT DISTINCT b FROM tb) s "
          "WHERE octet_length(b)=3",
          [[0]], "pushed below DISTINCT => 1"),

    # ---------------- P6: record image equality *= (named composite needed:
    # anonymous ROW() opr lookup collapses; single-field ROW degenerates)
    _case("eq_rec_win_imgeq",
          "P6: record_image_ops escape named in the clauses.c comment. "
          "*= is a btree member -> parsed as comparison, but its operand "
          "row(n,0)::nrec buries the Var -> uncaught. Window arm rep-free; "
          "the *=1.00 member row is kept, normalized n prints 1.0.",
          T_NUM_REC,
          "SELECT n, c FROM (SELECT n, count(*) OVER (PARTITION BY n) c "
          "FROM tn) s WHERE row(n,0)::nrec *= row(1.00::numeric,0)::nrec",
          [[1.0, 2]], "pushed => (1.0,1)"),
    _case("eq_rec_gb_imgeq_nonrep",
          "record *= over GROUP BY subquery, qual on non-rep image.",
          T_NUM_REC,
          "SELECT c FROM (SELECT n, count(*) c FROM tn GROUP BY n) s "
          "WHERE row(n,0)::nrec *= row(1.00::numeric,0)::nrec",
          [], "pushed => 1; rep flip => 2 (legal)"),
    _case("eq_rec_hav_imgeq_nonrep",
          "record *= x HAVING, qual on non-rep image.",
          T_NUM_REC,
          "SELECT count(*) c FROM tn GROUP BY n "
          "HAVING row(n,0)::nrec *= row(1.00::numeric,0)::nrec",
          [], "HAVING pushed => 1; rep flip => 2 (legal)"),
    _case("eq_rec_hav_imgeq_rep",
          "record *= x HAVING on the rep image: correct = 2.",
          T_NUM_REC,
          "SELECT count(*) c FROM tn GROUP BY n "
          "HAVING row(n,0)::nrec *= row(1.0::numeric,0)::nrec",
          [[2]], "pushed => 1"),

    # ---------------- container types
    _case("eq_arr_win_scale",
          "numeric[] '{1.0}'/'{1.00}' array_eq-equal; scale(a[1]) "
          "distinguishes (subscript+func buries the Var).",
          T_ARR,
          "SELECT a::text, c FROM (SELECT a, count(*) OVER "
          "(PARTITION BY a) c FROM ta) s WHERE scale(a[1])=1",
          [["{1.0}", 2]], "pushed => ('{1.0}',1)"),
    _case("eq_arr_gb_text",
          "numeric[] x GROUP BY, a::text qual on rep image.",
          T_ARR,
          "SELECT a::text, c FROM (SELECT a, count(*) c FROM ta "
          "GROUP BY a) s WHERE a::text='{1.0}'",
          [["{1.0}", 2]], "pushed => ('{1.0}',1); rep flip => empty"),
    _case("eq_arr_hav_scale",
          "numeric[] x HAVING.",
          T_ARR,
          "SELECT count(*) c FROM ta GROUP BY a HAVING scale(a[1])=1",
          [[2]], "pushed => 1; rep flip => 0 rows (legal)"),
    _case("eq_rng_win_scale",
          "numrange bound-scale: numrange(1.0,3)=numrange(1.00,3) under "
          "range_eq; scale(lower(r)) distinguishes.",
          T_RNG,
          "SELECT r::text, c FROM (SELECT r, count(*) OVER "
          "(PARTITION BY r) c FROM tr) s WHERE scale(lower(r))=1",
          [["[1.0,3)", 2]], "pushed => ('[1.0,3)',1)"),
    _case("eq_rng_hav_scale",
          "numrange x HAVING.",
          T_RNG,
          "SELECT count(*) c FROM tr GROUP BY r "
          "HAVING scale(lower(r))=1",
          [[2]], "pushed => 1; rep flip => 0 rows (legal)"),
    _case("eq_dom_win_scale",
          "domain-over-numeric: domain eq is numeric_eq; scale(n) "
          "implicitly casts down. Window arm rep-free.",
          T_DOM,
          "SELECT n, c FROM (SELECT n, count(*) OVER (PARTITION BY n) c "
          "FROM td) s WHERE scale(n)=1",
          [[1.0, 2]], "pushed => (1.0,1)"),
    _case("eq_dom_hav_scale",
          "domain-over-numeric x HAVING.",
          T_DOM,
          "SELECT count(*) c FROM td GROUP BY n HAVING scale(n)=1",
          [[2]], "pushed => 1; rep flip => 0 rows (legal)"),
    _case("eq_dom_distinct_text",
          "domain-over-numeric x DISTINCT, qual on non-rep image.",
          T_DOM,
          "SELECT count(*) FROM (SELECT DISTINCT n FROM td) s "
          "WHERE n::text='1.00'",
          [[0]], "pushed below DISTINCT => 1; rep flip => 1 (legal)"),

    # ---------------- DISTINCT ON (rep pinned by secondary ORDER BY) +
    # ---------------- setop dedup arms
    _case("eq_num_distinct_on",
          "DISTINCT ON (n) ORDER BY n,id pins rep to id=1 ('1.0'); qual "
          "scale(n)=2 fails on it => correct 0. Pushed removes id=1 "
          "pre-dedup => 1.00 survives => 1.",
          T_NUM,
          "SELECT count(*) FROM (SELECT DISTINCT ON (n) n, id FROM tn "
          "ORDER BY n, id) s WHERE scale(n)=2",
          [[0]], "pushed below DISTINCT ON => 1"),
    _case("eq_num_distinct_phantom",
          "plain DISTINCT phantom: dedup rep is '1.0' so '1.00' cannot be "
          "returned; pushdown resurrects it.",
          T_NUM,
          "SELECT n::text FROM (SELECT DISTINCT n FROM tn) s "
          "WHERE n::text='1.00'",
          [], "pushed => '1.00' phantom; rep flip => '1.00' legal"),
    _case("eq_num_union_text",
          "setop arm: UNION dedup + wrapper qual on non-rep image.",
          T_NUM,
          "SELECT n::text FROM (SELECT n FROM tn UNION SELECT n FROM tn) s "
          "WHERE n::text='1.00'",
          [], "pushed into UNION arms => '1.00' phantom"),
    _case("eq_num_isect_text",
          "setop arm: INTERSECT dedup.",
          T_NUM,
          "SELECT n::text FROM (SELECT n FROM tn INTERSECT "
          "SELECT n FROM tn) s WHERE n::text='1.00'",
          [], "pushed below INTERSECT => '1.00' phantom"),
    _case("eq_int_union_text",
          "setop arm on interval: rep '1 mon'; '30 days' cannot survive.",
          T_INT,
          "SELECT i::text FROM (SELECT i FROM ti UNION SELECT i FROM ti) s "
          "WHERE i::text='30 days'",
          [], "pushed => '30 days' phantom"),
    _case("eq_flt_union_text",
          "setop arm on float8: rep '0'; '-0' cannot survive.",
          T_FLT,
          "SELECT f::text FROM (SELECT f FROM tf UNION SELECT f FROM tf) s "
          "WHERE f::text='-0'",
          [], "pushed => '-0' phantom"),
    _case("eq_arr_union_text",
          "setop arm on numeric[].",
          T_ARR,
          "SELECT a::text FROM (SELECT a FROM ta UNION SELECT a FROM ta) s "
          "WHERE a::text='{1.00}'",
          [], "pushed => '{1.00}' phantom"),
    _case("eq_bp_union_octet3",
          "setop arm on bpchar via octet_length: correct 0 rows for any "
          "rep ('a  ' is never first-seen).",
          T_BP,
          "SELECT octet_length(b) FROM (SELECT b FROM tb UNION "
          "SELECT b FROM tb) s WHERE octet_length(b)=3",
          [], "pushed => len-3 phantom row"),

    # ---------------- clean control: bare-eq qual is a LEGIT pushdown
    _case("eq_num_gb_bareeq_ctl",
          "control: qual n = 1.0 uses the grouping's own equality — "
          "pushing it is legitimate and yields the same answer.",
          T_NUM,
          "SELECT n, c FROM (SELECT n, count(*) c FROM tn GROUP BY n) s "
          "WHERE n = 1.0::numeric",
          [[1.0, 2]], "any other result => anomaly"),
]
