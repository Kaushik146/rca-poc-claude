package com.aspire.audit;

import java.sql.*;
import java.util.ArrayList;
import java.util.List;

public class SqliteAuditLogger implements AuditLogger {

    private final String dbPath;

    public SqliteAuditLogger(String dbPath) {
        this.dbPath = dbPath;
        initSchema();
    }

    private Connection connect() throws SQLException {
        return DriverManager.getConnection("jdbc:sqlite:" + dbPath);
    }

    private void initSchema() {
        String sql = "CREATE TABLE IF NOT EXISTS audit_events (" +
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, " +
                     "event_type TEXT, order_id TEXT, customer_id TEXT, " +
                     "detail TEXT, timestamp_utc INTEGER)";   // INTEGER = epoch millis UTC
        try (Connection c = connect(); Statement st = c.createStatement()) {
            st.execute(sql);
        } catch (SQLException e) { throw new RuntimeException(e); }
    }

    @Override
    public void log(AuditEvent e) {
        String sql = "INSERT INTO audit_events (event_type,order_id,customer_id,detail,timestamp_utc) VALUES (?,?,?,?,?)";
        try (Connection c = connect(); PreparedStatement ps = c.prepareStatement(sql)) {
            ps.setString(1, e.getEventType());
            ps.setString(2, e.getOrderId());
            ps.setString(3, e.getCustomerId());
            ps.setString(4, e.getDetail());
            ps.setLong  (5, e.getTimestampUtc());
            ps.executeUpdate();
        } catch (SQLException ex) { throw new RuntimeException(ex); }
    }

    @Override
    public List<AuditEvent> findByOrderId(String orderId) {
        String sql = "SELECT * FROM audit_events WHERE order_id = ? ORDER BY timestamp_utc";
        List<AuditEvent> events = new ArrayList<>();
        try (Connection c = connect(); PreparedStatement ps = c.prepareStatement(sql)) {
            ps.setString(1, orderId);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    events.add(new AuditEvent(
                        rs.getString("event_type"), rs.getString("order_id"),
                        rs.getString("customer_id"), rs.getString("detail"),
                        rs.getLong("timestamp_utc")
                    ));
                }
            }
        } catch (SQLException e) { throw new RuntimeException(e); }
        return events;
    }
}
