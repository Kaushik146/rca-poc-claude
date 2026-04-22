package com.aspire.coupon;

public class CouponDTO {
    private String  code;
    private String  discountType;   // "PERCENTAGE" or "FIXED"
    private double  discountValue;  // e.g. 20.0 for 20%, or 15.0 for $15 off
    private double  minOrderValue;
    private boolean active;

    public CouponDTO() {}
    public CouponDTO(String code, String discountType, double discountValue,
                     double minOrderValue, boolean active) {
        this.code = code; this.discountType = discountType;
        this.discountValue = discountValue; this.minOrderValue = minOrderValue;
        this.active = active;
    }
    public String  getCode()          { return code; }
    public String  getDiscountType()  { return discountType; }
    public double  getDiscountValue() { return discountValue; }
    public double  getMinOrderValue() { return minOrderValue; }
    public boolean isActive()         { return active; }
    public void setCode(String code)                  { this.code = code; }
    public void setDiscountType(String discountType)  { this.discountType = discountType; }
    public void setDiscountValue(double v)            { this.discountValue = v; }
    public void setMinOrderValue(double v)            { this.minOrderValue = v; }
    public void setActive(boolean active)             { this.active = active; }
}
