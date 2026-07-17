# NSP Zeroconfig – Controller Code bootstrap

1. Người vận hành tạo trước bản ghi `nsp.controller` với `Controller Code` duy nhất.
2. Edge Server quảng bá Odoo URL qua IPv6 mDNS và ký TXT bằng Discovery Secret Key.
3. Controller nhập `Controller Code`, Service Type và Discovery Secret Key.
4. Controller xác minh quảng bá, chọn Edge Server và gửi yêu cầu HMAC đến `/nsp/zeroconfig/controller/bootstrap`.
5. Odoo chỉ chấp nhận Controller Code đã tồn tại, đang active và không bị block/revoked.
6. Odoo tự gán/tạo Core API Application khi cần, sinh route và cấp access/refresh token ngay.
7. Không có Controller Code Authentication Request, pending, approve, reject, polling hoặc cancel.
