package com.aspire.payment;
public class PaymentResult {
    private boolean success;
    private String  paymentRef;
    private String  errorMessage;

    public PaymentResult() {}
    public PaymentResult(boolean success, String paymentRef, String errorMessage) {
        this.success = success; this.paymentRef = paymentRef; this.errorMessage = errorMessage;
    }
    public static PaymentResult ok(String ref)   { return new PaymentResult(true,  ref,  null); }
    public static PaymentResult fail(String msg) { return new PaymentResult(false, null, msg); }
    public boolean isSuccess()       { return success; }
    public String  getPaymentRef()   { return paymentRef; }
    public String  getErrorMessage() { return errorMessage; }
}
