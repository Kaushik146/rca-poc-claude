package com.aspire.order;

import java.math.BigDecimal;
import java.sql.*;
import java.time.Instant;
import java.util.Optional;

/**
 * SQLite persistence for orders.
 *
 * Schema: id, customer_id, sku, quantity, total REAL, currency, status, created_at
 *
 * IMPORTANT: total is stored as REAL (64-bit float) — preserves decimal precision.
 * A common bug is storing as INTEGER which truncates $99.99 → $99.
 */
public class SqliteOrderRepository implements OrderRepository {

    private final String dbPath;

    public SqliteOrderRepository(String dbPath) {
        this.dbPath = dbPath;
        initSchema();
    }

    private Connection connect() throws SQLException {
        return DriverManager.getConnection("jdbc:sqlite:" + dbPath);
    }

    public void initSchema() {
        // total stored as REAL — preserves full decimal precision
        String sql = "CREATE TABLE IF NOT EXISTS orders (" +
                     "id TEXT PRIMARY KEY, " +
                     "customer_id TEXT NOT NULL, " +
                     "sku TEXT NOT NULL, " +
                     "quantity INTEGER NOT NULL, " +
                     "total REAL NOT NULL, " +
                     "currency TEXT NOT NULL, " +
                     "status TEXT NOT NULL, " +
                     "created_at INTEGER NOT NULL, " +
                     "idempotency_key TEXT)";
        try (Connection c = connect(); Statement st = c.createStatement()) {
            st.execute(sql);
            // Add idempotency_key column if it doesn't exist (for migration from older schema)
            try {
                st.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT");
            } catch (SQLException e) {
                // Column already exists, ignore
            }
        } catch (SQLException e) { throw new RuntimeException(e); }
    }

    @Override
    public void save(Order o) {
        String sql = "INSERT OR REPLACE INTO orders " +
                     "(id, customer_id, sku, quantity, total, currency, status, created_at, idempotency_key) " +
                     "VALUES (?,?,?,?,?,?,?,?,?)";
        try (Connection c = connect(); PreparedStatement ps = c.prepareStatement(sql)) {
            ps.setString(1, o.getId());
            ps.setString(2, o.getCustomerId());
            ps.setString(3, o.getSku());
            ps.setInt   (4, o.getQuantity());
            ps.setBigDecimal(5, o.getTotal());        // REAL — preserves $99.99 exactly
            ps.setString(6, o.getCurrency());
            ps.setString(7, o.getStatus().name());
            ps.setLong  (8, o.getCreatedAt().toEpochMilli());
            ps.setString(9, o.getIdempotencyKey());
            ps.executeUpdate();
        } catch (SQLException e) { throw new RuntimeException(e); }
    }

    @Override
    public Optional<Order> findById(String id) {
        String sql = "SELECT * FROM orders WHERE id = ?";
        try (Connection c = connect(); PreparedStatement ps = c.prepareStatement(sql)) {
            ps.setString(1, id);
            try (ResultSet rs = ps.executeQuery()) {
                if (!rs.next()) return Optional.empty();
                Order o = new Order(
                    rs.getString("id"),
                    rs.getString("customer_id"),
                    rs.getString("sku"),
                    rs.getInt("quantity"),
                    BigDecimal.valueOf(rs.getDouble("total")),
                    rs.getString("currency"),
                    OrderStatus.valueOf(rs.getString("status")),
                    Instant.ofEpochMilli(rs.getLong("created_at"))
                );
                o.setIdempotencyKey(rs.getString("idempotency_key"));
                return Optional.of(o);
            }
        } catch (SQLException e) { throw new RuntimeException(e); }
    }

    @Override
    public Optional<Order> findByIdempotencyKey(String idempotencyKey) {
        String sql = "SELECT * FROM orders WHERE idempotency_key = ?";
        try (Connection c = connect(); PreparedStatement ps = c.prepareStatement(sql)) {
            ps.setString(1, idempotencyKey);
            try (ResultSet rs = ps.executeQuery()) {
                if (!rs.next()) return Optional.empty();
                Order o = new Order(
                    rs.getString("id"),
                    rs.getString("customer_id"),
                    rs.getString("sku"),
                    rs.getInt("quantity"),
                    BigDecimal.valueOf(rs.getDouble("total")),
                    rs.getString("currency"),
                    OrderStatus.valueOf(rs.getString("status")),
                    Instant.ofEpochMilli(rs.getLong("created_at"))
                );
                o.setIdempotencyKey(rs.getString("idempotency_key"));
                return Optional.of(o);
            }
        } catch (SQLException e) { throw new RuntimeException(e); }
    }

    @Override
    public void updateStatus(String id, OrderStatus status) {
        String sql = "UPDATE orders SET status = ? WHERE id = ?";
        try (Connection c = connect(); PreparedStatement ps = c.prepareStatement(sql)) {
            ps.setString(1, status.name());
            ps.setString(2, id);
            ps.executeUpdate();
        } catch (SQLException e) { throw new RuntimeException(e); }
    }
}
