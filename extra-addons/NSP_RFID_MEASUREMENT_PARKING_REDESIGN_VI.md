# NSP RFID Tag Whitelist, Measurement và Parking Runtime

## 1. Phạm vi bản thiết kế

Bản source này dành cho **fresh install**. Các model, view, menu và route RFID legacy đã được loại bỏ; không giữ migration hoặc compatibility cho schema cũ.

Kiến trúc trách nhiệm:

- **Cloud**: master User, Vehicle, Friends, Vehicle Borrow, RFID Tag Whitelist, assignment, Reader Calibration và Parking Layout.
- **Edge**: bản sao runtime, nhận raw TID, ghép Directed Antenna Transition, quyết định Check-in/Check-out và tạo Parking Transaction.
- **Controller**: áp dụng cấu hình Reader, đọc TID và chuyển detection bền vững lên Edge; không xử lý nghiệp vụ người/xe.

## 2. RFID Tag Whitelist

Model `nsp.rfid.tag` chỉ có một trường nghiệp vụ: `tid`.

- TID được chuẩn hóa uppercase và bỏ whitespace.
- TID là duy nhất.
- Không lưu loại tag, mục đích, ghi chú hoặc trạng thái sử dụng.
- Vai trò Employee/Vehicle được suy ra từ assignment đang active.
- TID đã có lịch sử assignment không được đổi hoặc xóa.

Model `nsp.rfid.tag.assignment` lưu lịch sử cấp/revoke:

- Một TID chỉ có một assignment active.
- Một User chỉ có một Employee RFID Tag active.
- Một Vehicle chỉ có một RFID Tag active.
- Assignment đã revoke không được kích hoạt lại.
- Audit gồm `assigned_at`, `assigned_by_id`, `revoked_at`, `revoked_by_id`.

## 3. Giao diện User và Vehicle

- User form cho phép scan/nhập Employee TID và revoke tag. Không có tab RFID Assignment History; thao tác cấp/revoke được ghi vào Chatter chuẩn của Odoo, gồm người thao tác và thời điểm.
- Tab Vehicles trên User cho phép tạo nhanh phương tiện và cấp Vehicle TID.
- Không còn menu Vehicles độc lập.
- Model `nsp.vehicle` vẫn được giữ để phục vụ parking, mobile, history và Vehicle Borrow.
- RFID Tag Whitelist có menu riêng nhưng màn hình chỉ hiển thị TID.

## 4. Friends và Vehicle Borrow

Friends được giữ để chọn người mượn. Quan hệ Friend tự nó không tạo quyền lấy xe.

Check-out được phép khi User là:

1. Chủ sở hữu Vehicle; hoặc
2. Borrower có `nsp.vehicle.borrow` active, chưa return, và Event Time nằm trong `valid_from`–`valid_to`.

## 5. Reader Calibration

Reader Calibration sử dụng cặp target:

- Employee RFID Tag đang gắn active với User.
- Vehicle RFID Tag đang gắn active với Vehicle.
- User là owner hoặc borrower hợp lệ của Vehicle.

Measurement chỉ điều chỉnh tạm thời:

- Reader Power (dBm).
- Read Interval (ms).
- Antenna được chọn.

`TID Start Address` và `TID Length` thuộc Reader Operation Profile, không thuộc từng Measurement Session. Đơn vị là WORD 16-bit; 1 WORD = 2 byte.

`Apply to Operation` chỉ copy Power và Read Interval từ Measurement Reader Line vào Reader master. Việc ghi Reader master tự tăng Edge configuration revision.

Cloud chỉ đồng bộ các Measurement Session đang `ready` hoặc `running` xuống Edge. Terminal history được giữ tại Cloud, không ép Edge tái tạo target từ assignment đã revoke.

## 6. Parking Layout

Cloud quản lý:

- Branch → Parking Area → Lane → Controller → Reader → Antenna.
- Directed Antenna Transition: From Antenna, To Antenna, `event_type`, `duration_seconds`.
- Không dùng `direction`; `event_type` là `check_in` hoặc `check_out`.

Reader technical configuration gửi xuống Controller gồm:

- Power.
- Read Interval.
- TID Start Address (Words).
- TID Length (Words).
- Enabled Antennas.

## 7. Parking Runtime tại Edge

Controller gửi từng detection gồm `event_uid`, Reader serial, antenna, TID và timestamp.

Edge:

1. Chỉ nhận TID có active assignment.
2. Snapshot assignment xác định detection là User hoặc Vehicle.
3. Ghép hai Vehicle detections theo Directed Antenna Transition.
4. Check-in chỉ cần Vehicle Tag.
5. Check-out cần Vehicle Tag và Employee Tag trong cửa sổ Duration.
6. Khi chưa hết Duration, Edge chờ Employee Tag hợp lệ thay vì từ chối sớm vì một tag không được ủy quyền ở gần đó.
7. Tạo `nsp.parking.transaction` allowed/denied.
8. Chỉ Parking Transaction cuối cùng được đồng bộ lên Cloud; raw detection ở lại Edge và được dọn theo retention.

## 8. Quyết định lấy xe

Check-out allowed khi:

- Vehicle Tag có active assignment.
- Vehicle đang ở trong bãi theo allowed transaction cuối cùng.
- Employee Tag có active assignment.
- User là owner hoặc active borrower.
- Parking Area đang operational.

Nếu denied, Vehicle vẫn giữ trạng thái inside.

## 9. Cloud ↔ Edge Sync

Route RFID mới:

- `POST /nsp-sync/v1/rfid-tags/sync`

Payload là full snapshot:

```json
{
  "items": [
    {
      "tid": "E280689420005028A012A867",
      "assignment": {
        "target": "vehicle",
        "code": "VEH-...",
        "assigned_at": "2026-07-30T10:00:00+00:00"
      }
    }
  ]
}
```

Tag không có assignment chỉ gửi `tid`. Edge không xóa whitelist history; khi record biến mất khỏi snapshot, Edge chỉ revoke assignment active còn sót.

## 10. Device Whitelist và UI vận hành

`Device Whitelist` là nơi duy nhất được phép tạo hạ tầng thiết bị:

- Server.
- Controller.
- RFID Reader.
- Antenna.

Quan hệ bắt buộc: Server → Controller → RFID Reader → Antenna. Serial Number bắt buộc với RFID Reader và tùy chọn với ba loại còn lại. Technical Code hiển thị với Server và Controller. Device Type được trình bày bằng badge để phân biệt trực quan.

Các màn hình runtime Controller/Reader/Antenna chỉ đọc. `Parking Layout` và `Reader Calibration` chỉ chọn thiết bị active đã có trong Device Whitelist; không cho Quick Create hoặc Create and Edit thiết bị.

`Parking Layout` chỉ trình bày danh sách Lanes. Mỗi Lane chứa trực tiếp các Antenna Movement Rules có hướng From Antenna → To Antenna, Event Type và Duration. `Motorbike Capacity` đã bỏ. Số cột hiển thị được người vận hành điều chỉnh trực tiếp trên Live Monitor và lưu theo trình duyệt.
