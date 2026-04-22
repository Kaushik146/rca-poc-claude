package com.aspire.order;

public class CheckoutRequest {
    private String customerId;
    private String sku;           // product SKU
    private int    quantity;
    private double unitPrice;     // price per unit in USD
    private String couponCode;
    private String currency = "USD";
    private String idempotencyKey;

    public CheckoutRequest() {}
    public CheckoutRequest(String customerId, String sku, int quantity,
                           double unitPrice, String couponCode, String currency) {
        this.customerId = customerId;
        this.sku        = sku;
        this.quantity   = quantity;
        this.unitPrice  = unitPrice;
        this.couponCode = couponCode;
        this.currency   = currency != null ? currency : "USD";
    }

    public String getCustomerId() { return customerId; }
    public String getSku()        { return sku; }
    public int    getQuantity()   { return quantity; }
    public double getUnitPrice()  { return unitPrice; }
    public String getCouponCode() { return couponCode; }
    public String getCurrency()   { return currency; }
    public String getIdempotencyKey() { return idempotencyKey; }

    public void setCustomerId(String v) { this.customerId = v; }
    public void setSku(String v)        { this.sku = v; }
    public void setQuantity(int v)      { this.quantity = v; }
    public void setUnitPrice(double v)  { this.unitPrice = v; }
    public void setCouponCode(String v) { this.couponCode = v; }
    public void setCurrency(String v)   { this.currency = v; }
    public void setIdempotencyKey(String idempotencyKey) { this.idempotencyKey = idempotencyKey; }
}
