-- Copyright 2026 Snowflake Inc.
-- SPDX-License-Identifier: Apache-2.0
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
-- http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- DuckDB v2.0.0-alpha36050 (Cyanoptera) af1b4a9bd2 -- execution-layer INTERNAL Error, a v2.0 regression.
--
--   INTERNAL Error: Vector::Reference used on vector of different type
--   (source BIGINT referenced VARCHAR)
--
-- REGRESSION, and a stronger one than it looks: on released DuckDB 1.5.0 this exact script runs to
-- completion and returns the correct empty result. v2.0 aborts on a vector-type invariant instead.
--
-- Found by a differential fuzzer (row-multiset oracle) and delta-reduced from a 6-relation query with
-- two window functions, QUALIFY, a CASE/BETWEEN and an 8-statement equivalence chain -- none of which
-- turned out to be necessary.

CREATE TABLE b (id BIGINT, name VARCHAR, created_at VARCHAR);
INSERT INTO b VALUES (-3, 'a', 'a'), (NULL, 'b', 'b');
CREATE TABLE empt AS SELECT * FROM b WHERE 1 = 0;
CREATE VIEW t AS SELECT id, name, created_at FROM b ANTI JOIN empt ON TRUE;

SELECT t5.created_at
FROM (SELECT 'YEAR' AS c0
      FROM t AS t1 INNER JOIN t AS t2 ON t1.created_at <= t2.created_at
      WHERE COALESCE(t1.id, t2.id) IS NOT NULL) AS sq4
INNER JOIN t AS t5 ON sq4.c0 = t5.created_at;

-- ============================ what is and is not required ============================
-- Each line was tested individually; "no repro" means the script ran clean or produced an ordinary
-- (non-internal) error.
--
-- REQUIRED
--   * the scanned relation must contain a SEMI or ANTI JOIN. A plain table and a plain view both run
--     clean; `ANTI JOIN` and `SEMI JOIN` both reproduce, as does the ANTI JOIN written inline as three
--     derived tables instead of a view -- so this is not view-specific
--   * a self-join of that relation, joined on a VARCHAR comparison. `ON t1.created_at <= t2.created_at`
--     reproduces; `ON t1.id <= t2.id` (BIGINT) does NOT, which lines up with the BIGINT/VARCHAR pair in
--     the message. A CROSS JOIN does not reproduce either
--   * a filter over a null-coalesce across BOTH self-joined sides: COALESCE(t1.id, t2.id) or
--     IFNULL(t1.id, t2.id). A single-sided CAST(t1.id AS BOOLEAN), WHERE TRUE, and no WHERE all run clean
--   * at least one NULL in the coalesced column. **This is the trigger, not the row count**: two rows
--     with one NULL id reproduce, while eight rows with the NULL replaced by a literal 9 run clean
--   * the derived table must be joined to the outer relation on its CONSTANT column
--     (`sq4.c0 = t5.created_at`); joining on a non-constant column runs clean
--
-- NOT REQUIRED (all present in the original, none needed)
--   * any window function, QUALIFY, DISTINCT, CASE, BETWEEN or `||` -- selecting one bare column is enough
--   * FULL OUTER / RIGHT OUTER anywhere: plain INNER JOIN reproduces, unlike the sibling binder bug
--   * the CAST to BOOLEAN -- `IS NOT NULL` reproduces identically
--   * the extra projected columns, the third self-joined relation, the second outer join
--   * the QUALIFY view, the added-then-projected-away BIGINT column, and the TEMPORARY VIEW from the
--     original equivalence chain
--
-- ---------------------------------------------------------------------------------------
-- SEMI JOIN variant (same failure), showing it is the semi/anti family rather than ANTI specifically:
--
--   CREATE TABLE allrows AS SELECT * FROM b;
--   CREATE VIEW t AS SELECT id, name, created_at FROM b SEMI JOIN allrows ON TRUE;
--
-- Possibly related, but distinct: duckdb/duckdb#22274 (open) hits the same assertion with
-- BOOLEAN/TINYINT and diagnoses it as the MARK JOIN result vector conflicting with the source column
-- type in ConstructMarkJoinResult. Mark, semi and anti joins share that result-construction family, so
-- the same code may be involved -- but #22274 needs an Arrow-registered string_view column and an
-- IN (...) rewrite, neither of which is present here.
