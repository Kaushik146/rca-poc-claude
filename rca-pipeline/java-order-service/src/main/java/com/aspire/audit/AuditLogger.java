package com.aspire.audit;
import java.util.List;
public interface AuditLogger {
    void         log(AuditEvent event);
    List<AuditEvent> findByOrderId(String orderId);
}
