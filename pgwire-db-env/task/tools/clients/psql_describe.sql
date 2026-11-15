CREATE SCHEMA psq;
CREATE TABLE psq.item (id serial PRIMARY KEY, name varchar(40) NOT NULL UNIQUE, price numeric(8,2) DEFAULT 0 CHECK (price >= 0), tags text[], added timestamptz DEFAULT now());
CREATE TABLE psq.sale (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, item_id int REFERENCES psq.item(id) ON DELETE CASCADE, qty int NOT NULL);
CREATE INDEX sale_item_idx ON psq.sale (item_id);
CREATE VIEW psq.totals AS SELECT item_id, sum(qty) AS qty FROM psq.sale GROUP BY item_id;
CREATE FUNCTION psq.double(x int) RETURNS int LANGUAGE sql IMMUTABLE AS 'SELECT x * 2';
COMMENT ON TABLE psq.item IS 'things we sell';
\dt psq.*
\d psq.item
\d psq.sale
\d+ psq.totals
\di psq.*
\dv psq.*
\df psq.*
\ds psq.*
\dt+ psq.item
