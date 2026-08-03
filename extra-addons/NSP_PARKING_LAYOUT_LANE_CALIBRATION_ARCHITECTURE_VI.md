# NSP RFID, Lane Calibration và Parking Runtime

## 1. Phạm vi và nguyên tắc

Source này dành cho **fresh install**. Không giữ model, route, payload hoặc fallback chỉ phục vụ Parking schema cũ.

Phân chia trách nhiệm:

- **Cloud** quản lý Device Whitelist, Lane Calibration, Parking Layout và bản sao Parking Transaction.
- **Edge** nhận snapshot runtime, lưu raw RFID detection, đối chiếu chuỗi Antenna và tạo Parking Transaction.
- **Controller** chỉ áp dụng cấu hình Reader và gửi từng raw detection lên Edge. Controller không quyết định Check-in/Check-out.

## 2. RFID Tag và assignment

`nsp.rfid.tag` quản lý TID chuẩn hóa. Loại chủ thể được suy ra từ active assignment:

- Employee/User Tag liên kết với `nsp.user`.
- Vehicle Tag liên kết với `nsp.vehicle`.

Một TID chỉ có một assignment active. Lịch sử cấp và revoke được giữ để audit.

## 3. Lane Calibration

Lane Calibration đo cấu trúc vật lý của một Lane:

1. Chọn đúng thiết bị Server, Controller, Reader và Antenna.
2. Chọn Vehicle RFID Tags đã tồn tại trong hệ thống.
3. Controller gửi Calibration Observations theo Reader/Antenna và timestamp.
4. Cloud phân tích thứ tự phát hiện và Duration giữa các điểm liên tiếp.
5. Kết quả được Validate và Accept trước khi dùng cho Parking Layout.

Kết quả Calibration chỉ gồm:

- Antenna Timeline có thứ tự liên tục bắt đầu từ 1.
- Reader, Reader Serial, Antenna Port và Antenna Technical Code.
- Duration từ điểm trước và Cumulative Time.
- Timing Tolerance.

Lane Calibration **không tạo Check-in/Check-out Sequence** và không gán Event Type lên từng khoảng đo.

## 4. Parking Layout

Cấu trúc nghiệp vụ:

```text
Parking Area
└── Lane
    ├── Server + Controller
    ├── Antenna Timeline (import từ accepted Lane Calibration)
    ├── Check-in Sequence
    ├── Check-out Sequence
    └── Timing Tolerance
```

Khi import lại Calibration Timeline, hệ thống xóa Check-in/Check-out Sequence cũ. Người vận hành phải xác nhận lại chuỗi nghiệp vụ để không dùng nhầm cấu hình trên topology vật lý đã thay đổi.

Quy tắc:

- Timeline có ít nhất hai điểm.
- Order liên tục từ 1.
- Một Antenna chỉ xuất hiện một lần trong Timeline.
- Reader phải sở hữu Antenna và thuộc Lane Controller.
- Điểm đầu có Duration bằng 0; các điểm sau có Duration lớn hơn 0.
- Mỗi Event Sequence được cấu hình phải có ít nhất hai Antenna.
- Sequence chỉ dùng Antenna trong Timeline.
- Hai điểm liên tiếp trong Sequence phải kề nhau trên Timeline; có thể đi xuôi hoặc ngược.
- Nếu cấu hình đồng thời Check-in và Check-out, hai chuỗi phải đi ngược hướng nhau trên Timeline để loại bỏ kết quả nghiệp vụ mơ hồ.
- Một Antenna không được thuộc hai Lane operational.
- Lane operational phải có ít nhất một Check-in hoặc Check-out Sequence.

Không còn:

- `direction` trên Lane.
- Movement Rule/Directed Antenna Transition.
- `event_type` trên từng transition.
- Fallback từ Event Sequence sang Transition.

## 5. Publish và revision

Cloud lưu một immutable published payload cho mỗi Parking Layout.

- `Publish/Operational` kiểm tra toàn bộ topology rồi tạo revision mới từ bản Draft hiện tại.
- `Revise` đưa bản làm việc về Draft nhưng giữ immutable published revision trước đang hoạt động trên Edge.
- `Maintenance` và `Blocked` chỉ đổi trạng thái trên published snapshot gần nhất; không phát hành nội dung Draft đang sửa dở.
- Không thể chuyển Maintenance/Blocked nếu Layout chưa từng được Publish.
- Snapshot có `published_revision`; Edge bỏ qua revision cũ hơn revision đã áp dụng.
- Snapshot đầy đủ có cơ chế reconcile: Lane/Layout không còn trong snapshot sẽ bị deactivate hoặc blocked tại Edge.
- Published snapshot theo schema cũ bị từ chối rõ ràng; phải Revise, hoàn thiện Timeline/Sequences và Publish lại.

Payload Lane chuẩn. Reader/Antenna configuration được gửi một lần tại `controllers[].devices`, không lặp lại trong từng Lane:

```json
{
  "lane_code": "LANE_01",
  "lane_name": "Entry Lane",
  "server_code": "EDGE_01",
  "controller_code": "CTRL_01",
  "antenna_timeline": [],
  "event_sequences": {
    "check_in": [],
    "check_out": []
  },
  "timing_tolerance": {
    "type": "percent",
    "value": 30.0
  }
}
```

## 6. Parking Runtime tại Edge

Controller gửi từng detection gồm:

- `event_uid`.
- Reader Serial Number.
- Antenna Number.
- TID.
- UTC timestamp.

Edge xử lý:

1. Xác thực idempotency bằng `event_uid`.
2. Tra active RFID assignment để xác định User hoặc Vehicle.
3. Phân giải Reader/Antenna vào đúng một operational Lane Timeline.
4. Gom các detection pending theo Lane và Tag.
5. Loại các lần đọc lặp liên tiếp trên cùng Antenna.
6. So khớp đúng Check-in/Check-out Sequence đã cấu hình.
7. Kiểm tra Duration của từng cặp Antenna kề nhau theo Calibration và Tolerance.
8. Tạo một `nsp.parking.transaction` allowed/denied.
9. Chỉ đồng bộ final Parking Transaction lên Cloud; raw detections ở lại Edge và được cleanup theo retention.

Check-in chỉ cần Vehicle Tag. Check-out cần Employee Tag phù hợp trong configured sequence window; User phải là owner hoặc active borrower.

## 7. API đồng bộ Cloud ↔ Edge

Các route Parking/Lane Calibration chuẩn, sau khi Core API thêm version prefix:

| Direction | Method | Route |
|---|---|---|
| Edge → Cloud | POST | `/v1/edge/status` |
| Cloud → Edge | POST | `/v1/edge/parking-runtime/snapshot` |
| Cloud → Edge | POST | `/v1/edge/lane-calibrations/snapshot` |
| Edge → Cloud | POST | `/v1/edge/lane-calibrations/events` |
| Edge → Cloud | POST | `/v1/edge/lane-calibrations/status` |
| Edge → Cloud | POST | `/v1/edge/parking-transactions` |

Route suffix phía Odoo không chứa `/v1`; `t4_coreapi` chịu trách nhiệm version prefix.

## 8. Route bị loại bỏ

| Route cũ | Route mới |
|---|---|
| `edge-server/status` | `edge/status` |
| `gatekeeper-config/sync` | `edge/parking-runtime/snapshot` |
| `measurement-config/sync` | `edge/lane-calibrations/snapshot` |
| `measurement-events/sync` | `edge/lane-calibrations/events` |
| `measurement-status/sync` | `edge/lane-calibrations/status` |
| `parking-transactions/sync` | `edge/parking-transactions` |

Đây là breaking API contract. Controller application chưa được sửa trong bản này.

## 9. Idempotency và transaction integrity

- Raw detection: `event_uid` unique; cùng UID khác dữ liệu bị từ chối.
- Parking Transaction: `transaction_uid` unique trên Edge và Cloud.
- Cloud không chạy lại topology decision cho transaction đến trễ; topology snapshot trong transaction là bằng chứng nghiệp vụ tại thời điểm Edge quyết định.
- Mỗi record xử lý trong savepoint riêng; lỗi một item không rollback toàn batch.
- Sync Record lưu pending/synced/failed để retry bền vững.

## 10. Clean Code

Đã loại bỏ:

- Model `nsp.parking.antenna.transition`.
- `parking_sequence.py` và `parking_sequence_detection.py` legacy.
- Access rules và view field của Transition/Direction.
- Payload `antenna_transitions` và fallback schema cũ.
- Tên biến `transition_count`; thay bằng `timeline_point_count`.

Canonical source chỉ còn một Parking engine dựa trên Timeline + Event Sequences.
