// pgJDBC 42.7.4 against the server under test. Prints one JSON object {item: result}.
//   java -cp /opt/clients/postgresql-42.7.4.jar JdbcSuite.java <host> [port]
// Each item opens its own connection; failures are recorded as "ERR <sqlstate class>" or "ERR <exception>".
import java.io.*;
import java.math.BigDecimal;
import java.sql.*;
import java.util.*;
import org.postgresql.PGConnection;
import org.postgresql.copy.CopyManager;

public class JdbcSuite {
    static String host;
    static int port;

    static Connection conn(String user, String pw, String extra) throws SQLException {
        Properties p = new Properties();
        p.setProperty("user", user);
        if (pw != null) p.setProperty("password", pw);
        p.setProperty("connectTimeout", "10");
        p.setProperty("socketTimeout", "30");
        p.setProperty("sslmode", "disable");
        return DriverManager.getConnection("jdbc:postgresql://" + host + ":" + port + "/regression" + extra, p);
    }

    static Connection conn() throws SQLException { return conn("postgres", null, ""); }

    static String q(Object o) {
        if (o == null) return "null";
        if (o instanceof Number || o instanceof Boolean) return o.toString();
        if (o instanceof List) {
            StringBuilder b = new StringBuilder("[");
            for (Object x : (List<?>) o) { if (b.length() > 1) b.append(","); b.append(q(x)); }
            return b.append("]").toString();
        }
        String s = o.toString().replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\t", "\\t");
        return "\"" + s + "\"";
    }

    interface Item { Object run() throws Exception; }

    static List<Object> row(ResultSet rs) throws SQLException {
        List<Object> r = new ArrayList<>();
        for (int i = 1; i <= rs.getMetaData().getColumnCount(); i++) r.add(rs.getString(i));
        return r;
    }

    static List<Object> all(ResultSet rs) throws SQLException {
        List<Object> out = new ArrayList<>();
        while (rs.next()) out.add(row(rs));
        return out;
    }

    public static void main(String[] a) {
        host = a[0];
        port = a.length > 1 ? Integer.parseInt(a[1]) : 5432;
        LinkedHashMap<String, Item> items = new LinkedHashMap<>();

        items.put("scram_auth", () -> {
            try (Connection c = conn("alice", "wonderland", "")) {
                return all(c.createStatement().executeQuery("SELECT current_user"));
            }
        });
        items.put("simple_statement", () -> {
            try (Connection c = conn()) {
                return all(c.createStatement().executeQuery("SELECT 1 AS a, 'x'::text AS b, NULL::int AS c"));
            }
        });
        items.put("server_prepared_binary", () -> {
            // prepareThreshold=1: named server statements from the first execution; binary transfer for ints/dates.
            try (Connection c = conn("postgres", null, "?prepareThreshold=1")) {
                c.createStatement().execute("CREATE TABLE jd_p (id int PRIMARY KEY, v text, n numeric(8,2), d date, ts timestamp)");
                PreparedStatement ins = c.prepareStatement("INSERT INTO jd_p VALUES (?, ?, ?, ?, ?)");
                for (int i = 0; i < 8; i++) {
                    ins.setInt(1, i); ins.setString(2, "v" + i); ins.setBigDecimal(3, new BigDecimal(i + ".25"));
                    ins.setDate(4, java.sql.Date.valueOf("2024-01-0" + (i + 1)));
                    ins.setTimestamp(5, java.sql.Timestamp.valueOf("2024-01-01 10:00:0" + i));
                    ins.executeUpdate();
                }
                PreparedStatement sel = c.prepareStatement("SELECT id, v, n, d, ts FROM jd_p WHERE id >= ? ORDER BY id");
                List<Object> out = new ArrayList<>();
                for (int k = 0; k < 6; k++) { sel.setInt(1, 6); out.add(all(sel.executeQuery())); }
                return out;
            }
        });
        items.put("batch_insert", () -> {
            try (Connection c = conn()) {
                c.createStatement().execute("CREATE TABLE jd_b (id int PRIMARY KEY, v text)");
                PreparedStatement ps = c.prepareStatement("INSERT INTO jd_b VALUES (?, ?)");
                for (int i = 0; i < 300; i++) { ps.setInt(1, i); ps.setString(2, "r" + i); ps.addBatch(); }
                int[] counts = ps.executeBatch();
                int sum = 0; for (int x : counts) sum += x;
                return List.of(counts.length, sum, all(c.createStatement().executeQuery("SELECT count(*), max(v) FROM jd_b")));
            }
        });
        items.put("batch_with_error", () -> {
            try (Connection c = conn()) {
                c.createStatement().execute("CREATE TABLE jd_be (id int PRIMARY KEY)");
                c.setAutoCommit(false);
                PreparedStatement ps = c.prepareStatement("INSERT INTO jd_be VALUES (?)");
                for (int i : new int[]{1, 2, 2, 3}) { ps.setInt(1, i); ps.addBatch(); }
                String st = "none";
                try { ps.executeBatch(); } catch (BatchUpdateException e) { st = e.getSQLState(); }
                c.rollback();
                c.setAutoCommit(true);
                return List.of(st, all(c.createStatement().executeQuery("SELECT count(*) FROM jd_be")));
            }
        });
        items.put("typed_getters", () -> {
            try (Connection c = conn()) {
                ResultSet rs = c.createStatement().executeQuery("SELECT 2147483648::int8, 12.345::numeric(6,3), 0.5::float8, true, "
                    + "'2024-02-29'::date, '\\x0102'::bytea, ARRAY[1,2,3], 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid, '13:14:15'::time");
                rs.next();
                Integer[] arr = (Integer[]) rs.getArray(7).getArray();
                return List.of(rs.getLong(1), rs.getBigDecimal(2).toPlainString(), rs.getDouble(3), rs.getBoolean(4),
                    rs.getDate(5).toString(), Arrays.toString(rs.getBytes(6)), Arrays.asList(arr), rs.getObject(8).toString(),
                    rs.getTime(9).toString());
            }
        });
        items.put("result_metadata", () -> {
            try (Connection c = conn()) {
                c.createStatement().execute("CREATE TABLE jd_m (id int NOT NULL, amount numeric(10,2), name varchar(30))");
                ResultSetMetaData m = c.createStatement().executeQuery("SELECT id, amount, name, 1 AS lit FROM jd_m").getMetaData();
                List<Object> out = new ArrayList<>();
                for (int i = 1; i <= m.getColumnCount(); i++)
                    out.add(List.of(m.getColumnName(i), m.getColumnTypeName(i), m.getPrecision(i), m.getScale(i), m.isNullable(i),
                        m.getTableName(i)));
                return out;
            }
        });
        items.put("database_metadata_tables", () -> {
            try (Connection c = conn()) {
                c.createStatement().execute("CREATE TABLE jd_meta_a (x int); CREATE TABLE jd_meta_b (y text)");
                ResultSet rs = c.getMetaData().getTables(null, "public", "jd_meta_%", new String[]{"TABLE"});
                List<Object> out = new ArrayList<>();
                while (rs.next()) out.add(List.of(rs.getString("TABLE_NAME"), rs.getString("TABLE_TYPE")));
                ResultSet cols = c.getMetaData().getColumns(null, "public", "jd_meta_b", "%");
                while (cols.next()) out.add(List.of(cols.getString("COLUMN_NAME"), cols.getString("TYPE_NAME")));
                return out;
            }
        });
        items.put("generated_keys", () -> {
            try (Connection c = conn()) {
                c.createStatement().execute("CREATE TABLE jd_g (id serial PRIMARY KEY, v text)");
                PreparedStatement ps = c.prepareStatement("INSERT INTO jd_g (v) VALUES (?)", Statement.RETURN_GENERATED_KEYS);
                ps.setString(1, "a"); ps.executeUpdate();
                ps.setString(1, "b"); ps.executeUpdate();
                return all(ps.getGeneratedKeys());
            }
        });
        items.put("transactions_savepoints", () -> {
            try (Connection c = conn()) {
                c.createStatement().execute("CREATE TABLE jd_t (v int)");
                c.setAutoCommit(false);
                Statement s = c.createStatement();
                s.execute("INSERT INTO jd_t VALUES (1)");
                Savepoint sp = c.setSavepoint();
                s.execute("INSERT INTO jd_t VALUES (2)");
                c.rollback(sp);
                s.execute("INSERT INTO jd_t VALUES (3)");
                c.commit();
                s.execute("INSERT INTO jd_t VALUES (4)");
                c.rollback();
                c.setAutoCommit(true);
                return all(s.executeQuery("SELECT v FROM jd_t ORDER BY v"));
            }
        });
        items.put("fetch_size_portal", () -> {
            // autocommit off + fetchSize: Execute with a row limit, PortalSuspended, more Executes.
            try (Connection c = conn()) {
                c.setAutoCommit(false);
                Statement s = c.createStatement();
                s.setFetchSize(7);
                ResultSet rs = s.executeQuery("SELECT g FROM generate_series(1, 50) g");
                long sum = 0; int n = 0;
                while (rs.next()) { sum += rs.getLong(1); n++; }
                c.commit();
                return List.of(n, sum);
            }
        });
        items.put("sqlstate_errors", () -> {
            try (Connection c = conn()) {
                List<Object> out = new ArrayList<>();
                for (String sql : new String[]{"SELECT 1/0", "SELECT * FROM jd_missing", "SELEC 1"}) {
                    try { c.createStatement().execute(sql); out.add("none"); } catch (SQLException e) { out.add(e.getSQLState()); }
                }
                return out;
            }
        });
        items.put("copy_manager", () -> {
            try (Connection c = conn()) {
                c.createStatement().execute("CREATE TABLE jd_c (id int, v text)");
                CopyManager cm = ((PGConnection) c).getCopyAPI();
                long in = cm.copyIn("COPY jd_c FROM STDIN WITH (FORMAT csv)", new StringReader("1,one\n2,\"t,wo\"\n3,\n"));
                StringWriter w = new StringWriter();
                long out = cm.copyOut("COPY (SELECT * FROM jd_c ORDER BY id) TO STDOUT", w);
                return List.of(in, out, w.toString());
            }
        });
        items.put("query_timeout_cancel", () -> {
            try (Connection c = conn()) {
                Statement s = c.createStatement();
                s.setQueryTimeout(1);
                long t0 = System.currentTimeMillis();
                String st = "none";
                try { s.execute("SELECT pg_sleep(15)"); } catch (SQLException e) { st = e.getSQLState(); }
                return List.of(st, System.currentTimeMillis() - t0 < 6000);
            }
        });

        StringBuilder out = new StringBuilder("{");
        for (Map.Entry<String, Item> e : items.entrySet()) {
            String v;
            try {
                Object r = e.getValue().run();
                v = q(r instanceof List ? r : List.of(String.valueOf(r)));
            } catch (SQLException ex) {
                v = q("ERR " + (ex.getSQLState() == null ? "null" : ex.getSQLState().substring(0, Math.min(2, ex.getSQLState().length()))));
            } catch (Throwable ex) {
                v = q("ERR " + ex.getClass().getSimpleName());
            }
            if (out.length() > 1) out.append(",");
            out.append(q(e.getKey())).append(":").append(v);
        }
        System.out.println(out.append("}"));
    }
}
