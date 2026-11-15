// node-postgres (pg 8.13.1) against the server under test. Prints one JSON object {item: result}.
// Each item opens its own client; errors are recorded as "ERR <sqlstate class>".
//   node node_suite.js <host> [port]
process.env.TZ = 'UTC';
const { Client, Pool } = require('/opt/clients/node/node_modules/pg');

const HOST = process.argv[2];
const PORT = parseInt(process.argv[3] || '5432', 10);
const cfg = (user = 'postgres', password = undefined) =>
  ({ host: HOST, port: PORT, user, password, database: 'regression', connectionTimeoutMillis: 10000 });

async function withClient(fn, user, password) {
  const c = new Client(cfg(user, password));
  await c.connect();
  try { return await fn(c); } finally { await c.end().catch(() => {}); }
}

const render = (v) => {
  if (v === null || v === undefined) return null;
  if (Buffer.isBuffer(v)) return 'bytes:' + v.toString('hex');
  if (v instanceof Date) return 'date:' + (isNaN(v) ? 'invalid' : v.toISOString());
  if (Array.isArray(v)) return v.map(render);
  if (typeof v === 'object') { const o = {}; for (const k of Object.keys(v).sort()) o[k] = render(v[k]); return o; }
  return v;
};

const items = {
  async simple_protocol_query() {
    return withClient(async (c) => {
      const r = await c.query("SELECT 1 AS x, 'a'::text AS y, NULL::int AS z");
      return [r.command, r.rowCount, r.fields.map((f) => [f.name, f.dataTypeID]), render(r.rows)];
    });
  },
  async multi_statement_simple() {
    return withClient(async (c) => {
      const rs = await c.query('SELECT 1 AS a; SELECT 2 AS b, 3 AS c');
      return rs.map((r) => [r.command, render(r.rows)]);
    });
  },
  async parameterized_unnamed() {
    return withClient(async (c) => {
      const r = await c.query('SELECT $1::int + $2::int AS s, $3::text AS t, $4::text IS NULL AS n', [40, 2, "it's", null]);
      return render(r.rows);
    });
  },
  async named_prepared_reuse() {
    return withClient(async (c) => {
      await c.query('CREATE TABLE np_t (id int PRIMARY KEY, v text)');
      const out = [];
      for (let i = 0; i < 5; i++) {
        await c.query({ name: 'np-insert', text: 'INSERT INTO np_t VALUES ($1, $2)', values: [i, 'v' + i] });
        const r = await c.query({ name: 'np-select', text: 'SELECT v FROM np_t WHERE id = $1', values: [i] });
        out.push(r.rows[0].v);
      }
      return out;
    });
  },
  async type_parsing() {
    return withClient(async (c) => {
      const r = await c.query(`SELECT 9007199254740993::int8 AS i8, 1.10::numeric(5,2) AS n, 2.5::float8 AS f, true AS b,
        '2024-02-29'::date AS d, '2024-02-29 12:00:00+02'::timestamptz AS tz, '2024-02-29 12:00:00'::timestamp AS ts,
        '{"a":[1,2]}'::json AS j, '{"b":{"c":null}}'::jsonb AS jb, ARRAY[1,2,NULL] AS ai, ARRAY['x','y z'] AS at,
        '\\xdeadbeef'::bytea AS by, '1 day 02:00:00'::interval AS iv, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid AS u`);
      return render(r.rows[0]);
    });
  },
  async row_mode_array() {
    return withClient(async (c) => {
      const r = await c.query({ text: 'SELECT g, g * 2 FROM generate_series(1, 3) g', rowMode: 'array' });
      return render(r.rows);
    });
  },
  async errors() {
    return withClient(async (c) => {
      await c.query('CREATE TABLE ne_t (id int PRIMARY KEY)');
      await c.query('INSERT INTO ne_t VALUES (1)');
      const out = [];
      for (const q of ['INSERT INTO ne_t VALUES (1)', 'SELECT * FROM ne_missing', 'SELECT 1/0', 'SELEC 1']) {
        try { await c.query(q); out.push('no error'); } catch (e) {
          out.push([e.code, e.severity, e.constraint || null, e.position !== undefined]);
        }
      }
      out.push(render((await c.query('SELECT 1 AS still_ok')).rows));
      return out;
    });
  },
  async transactions() {
    return withClient(async (c) => {
      await c.query('CREATE TABLE nt_t (v int)');
      await c.query('BEGIN');
      await c.query('INSERT INTO nt_t VALUES ($1)', [1]);
      await c.query('SAVEPOINT s');
      await c.query('INSERT INTO nt_t VALUES ($1)', [2]);
      await c.query('ROLLBACK TO SAVEPOINT s');
      await c.query('COMMIT');
      await c.query('BEGIN');
      await c.query('INSERT INTO nt_t VALUES (3)');
      try { await c.query('SELECT 1/0'); } catch (e) { /* aborted */ }
      const commit = await c.query('COMMIT');
      return [commit.command, render((await c.query('SELECT array_agg(v) AS a FROM nt_t')).rows)];
    });
  },
  async listen_notify() {
    const l = new Client(cfg()); const t = new Client(cfg());
    await l.connect(); await t.connect();
    try {
      const got = [];
      const done = new Promise((resolve) => {
        l.on('notification', (m) => { got.push([m.channel, m.payload, m.processId === t.processID]); if (got.length === 2) resolve(); });
        setTimeout(resolve, 5000);
      });
      await l.query('LISTEN node_chan');
      await t.query("NOTIFY node_chan, 'one'");
      await t.query("SELECT pg_notify('node_chan', 'two')");
      await done;
      return got;
    } finally { await l.end().catch(() => {}); await t.end().catch(() => {}); }
  },
  async scram_auth() {
    return withClient(async (c) => render((await c.query('SELECT current_user AS u')).rows), 'alice', 'wonderland');
  },
  async md5_auth() {
    return withClient(async (c) => render((await c.query('SELECT current_user AS u')).rows), 'bob', 'builder');
  },
  async pool_concurrency() {
    const pool = new Pool({ ...cfg(), max: 10 });
    try {
      const rs = await Promise.all([...Array(40).keys()].map((i) => pool.query('SELECT $1::int * 3 AS v, pg_sleep(0.05)', [i])));
      return rs.map((r) => r.rows[0].v);
    } finally { await pool.end(); }
  },
};

(async () => {
  const out = {};
  for (const [name, fn] of Object.entries(items)) {
    try {
      out[name] = await Promise.race([fn(), new Promise((_, rej) => setTimeout(() => rej(new Error('item timeout')), 30000))]);
    } catch (e) {
      out[name] = e.code ? 'ERR ' + String(e.code).slice(0, 2) : 'ERR ' + (e.message || String(e)).slice(0, 60);
    }
  }
  console.log(JSON.stringify(out));
  process.exit(0);
})();
