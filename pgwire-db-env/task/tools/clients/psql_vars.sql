SELECT 42 AS answer, 'hi' AS greeting \gset
\echo :answer :greeting
SELECT :answer + 1 AS next;
\set tbl pv_t
CREATE TABLE :tbl (a int);
INSERT INTO :"tbl" VALUES (:answer);
SELECT * FROM :tbl;
SELECT count(*) > 0 AS has_rows FROM pv_t \gset
\if :has_rows
  \echo 'has rows'
\else
  \echo 'empty'
\endif
SELECT format('SELECT %s AS gexec_result', g) FROM generate_series(1, 2) g \gexec
SELECT 1 / 0;
\echo :ERROR :LAST_ERROR_SQLSTATE
BEGIN;
SELECT 1;
\echo :ROW_COUNT
COMMIT;
