CREATE TABLE pf (id int, label text, amount numeric(6,2), note text);
INSERT INTO pf VALUES (1, 'alpha', 1.5, NULL), (2, 'multi
line', -20, 'x|y'), (3, '', 1000, 'ünïcode');
SELECT * FROM pf ORDER BY id;
\x on
SELECT * FROM pf ORDER BY id;
\x off
\pset format csv
SELECT * FROM pf ORDER BY id;
\pset format unaligned
\pset fieldsep ';'
SELECT * FROM pf ORDER BY id;
\pset format aligned
\pset null '(null)'
\pset border 2
SELECT * FROM pf ORDER BY id;
\pset format html
SELECT id, label FROM pf WHERE id = 1;
\pset format aligned
\pset border 1
\pset null ''
SELECT label, id, amount FROM pf ORDER BY id \crosstabview
