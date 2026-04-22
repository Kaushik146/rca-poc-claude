package com.aspire.coupon;
import java.util.Optional;
public interface CouponServiceClient {
    Optional<CouponDTO> getCoupon(String code);
}
