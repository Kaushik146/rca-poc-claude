package com.aspire.order;

import java.math.BigDecimal;
import java.time.Instant;

public class Order {
    private String      id;
    private String      customerId;
    private String      sku;
    private int         quantity;
    private BigDecimal total;
    private String      currency;
    private OrderStatus status;
    private Instant     createdAt;
    private String      idempotencyKey;

    public Order() {}
    public Order(String id, String customerId, String sku, int quantity,
                 BigDecimal total, String currency, OrderStatus status, Instant createdAt) {
        this.id = id; this.customerId = customerId; this.sku = sku;
        this.quantity = quantity; this.total = total; this.currency = currency;
        this.status = status; this.createdAt = createdAt;
    }

    public String      getId()         { return id; }
    public String      getOrderId()    { return id; }
    public String      getCustomerId() { return customerId; }
    public String      getSku()        { return sku; }
    public int         getQuantity()   { return quantity; }
    public BigDecimal getTotal()      { return total; }
    public String      getCurrency()   { return currency; }
    public OrderStatus getStatus()     { return status; }
    public Instant     getCreatedAt()  { return createdAt; }
    public String      getIdempotencyKey() { return idempotencyKey; }

    public void setId(String id)             { this.id = id; }
    public void setCustomerId(String v)      { this.customerId = v; }
    public void setSku(String v)             { this.sku = v; }
    public void setQuantity(int v)           { this.quantity = v; }
    public void setTotal(BigDecimal v)     { this.total = v; }
    public void setCurrency(String v)        { this.currency = v; }
    public void setStatus(OrderStatus v)     { this.status = v; }
    public void setCreatedAt(Instant v)      { this.createdAt = v; }
    public void setIdempotencyKey(String idempotencyKey) { this.idempotencyKey = idempotencyKey; }
}
