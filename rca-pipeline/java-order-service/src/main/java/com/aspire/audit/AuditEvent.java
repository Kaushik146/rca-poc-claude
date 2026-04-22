package com.aspire.audit;

public class AuditEvent {
    private String eventType;
    private String orderId;
    private String customerId;
    private String detail;
    private long   timestampUtc;   // epoch millis UTC

    public AuditEvent() {}

    /** 5-param constructor with explicit timestamp. */
    public AuditEvent(String eventType, String orderId, String customerId,
                      String detail, long timestampUtc) {
        this.eventType    = eventType;
        this.orderId      = orderId;
        this.customerId   = customerId;
        this.detail       = detail;
        this.timestampUtc = timestampUtc;
    }

    /** 4-param constructor — auto-sets timestamp to current UTC millis. */
    public AuditEvent(String eventType, String orderId, String customerId, String detail) {
        this(eventType, orderId, customerId, detail, System.currentTimeMillis());
    }

    public String getEventType()    { return eventType; }
    public String getOrderId()      { return orderId; }
    public String getCustomerId()   { return customerId; }
    public String getDetail()       { return detail; }
    public long   getTimestampUtc() { return timestampUtc; }
}
