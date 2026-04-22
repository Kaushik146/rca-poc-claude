package com.aspire.order;

public class CheckoutResult {
    private final boolean success;
    private final String  orderId;
    private final double  total;
    private final String  currency;
    private final String  errorMessage;

    private CheckoutResult(boolean success, String orderId, double total,
                           String currency, String errorMessage) {
        this.success      = success;
        this.orderId      = orderId;
        this.total        = total;
        this.currency     = currency;
        this.errorMessage = errorMessage;
    }

    public static CheckoutResult success(String orderId, double total, String currency) {
        return new CheckoutResult(true, orderId, total, currency, null);
    }

    public static CheckoutResult failure(String orderId, String errorMessage) {
        return new CheckoutResult(false, orderId, 0.0, null, errorMessage);
    }

    public boolean isSuccess()      { return success; }
    public String  getOrderId()     { return orderId; }
    public double  getTotal()       { return total; }
    public String  getCurrency()    { return currency; }
    public String  getErrorMessage(){ return errorMessage; }

    @Override public String toString() {
        return success
            ? "CheckoutResult{OK orderId=" + orderId + " total=" + total + " " + currency + "}"
            : "CheckoutResult{FAIL reason=" + errorMessage + "}";
    }
}
