CREATE TABLE pc (id int, v text, d date);
\copy pc FROM '/work/tools/clients/psql_copy.data'
\copy pc FROM '/work/tools/clients/psql_copy.csv' WITH (FORMAT csv, HEADER)
SELECT * FROM pc ORDER BY id;
\copy (SELECT id, v FROM pc WHERE id > 2 ORDER BY id) TO STDOUT
\copy pc TO STDOUT WITH (FORMAT csv)
COPY (SELECT count(*) FROM pc) TO STDOUT;
