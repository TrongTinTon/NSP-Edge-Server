# NSP Parking Monitor Notification Architecture

Chốt kiến trúc:

```text
NSP Gatekeeper
→ source of truth: nsp.parking.transaction
→ xác định xe/người, gate, direction, allowed/denied

NSP Notification
→ tạo notification từ parking transaction
→ channel: parking_monitor
→ phát payload gọn cho màn hình bãi xe

NSP Parking Realtime
→ màn hình/client action
→ lấy event từ notification layer
→ không tự sở hữu nghiệp vụ parking
```

## Nguyên tắc

- `nsp.parking.transaction` là dữ liệu nghiệp vụ gốc.
- `nsp.notification` là lớp phát sự kiện/hiển thị.
- Parking monitor là notification channel, không phải model nghiệp vụ mới.
- Không lưu raw controller payload trong notification.
- Payload monitor chỉ chứa dữ liệu cần hiển thị: biển số/TID, gate, branch, direction, status, owner, message.

## Endpoint chính thức cho màn hình ngoài Odoo

Tất cả monitor chạy ngoài backend Odoo, bao gồm kiosk/browser treo ở bãi xe, phải đi qua Core API Application.

```text
POST /auth/token?db=<dbname>
GET  /parking-monitor/v1/parking-monitor/events?db=<dbname>
Authorization: Bearer <access_token>
```

Core API Application mặc định:

```text
Name: NSP Parking Monitor
Server Code: parking-monitor
Allowed route: parking-monitor/events
```

Endpoint nội bộ `/api/nsp_notification/v1/parking-monitor/events` chỉ dành cho Odoo backend/client action đã đăng nhập, không dùng cho thiết bị ngoài.

Query hỗ trợ:

```text
gate_id
branch_id
direction=entry|exit
status=allowed|denied
since_id
limit
```

Response dùng `source_model = nsp.notification` và `business_source_model = nsp.parking.transaction`.

## Quy tắc bảo mật

- Mobile app, parking monitor, kiosk và browser ngoài Odoo đều là external client.
- External client không gọi direct Odoo controller.
- External client phải có `core.api.application`, `client_id`, `client_secret`, access token, rate limit và IP allowlist nếu cần.
- Notification chỉ cung cấp payload/event; Core API quản lý Application, token, route authorization và log.
