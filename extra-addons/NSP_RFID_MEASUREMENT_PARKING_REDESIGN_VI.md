# NSP RFID Tag Whitelist, Measurement và Parking Runtime

## 1. Phạm vi bản thiết kế

Bản source này dành cho **fresh install**. Các model, view, menu và route RFID legacy đã được loại bỏ; không giữ migration hoặc compatibility cho schema cũ.

Kiến trúc trách nhiệm:

- **Cloud**: master User, Vehicle, Friends, Vehicle Borrow, RFID Tag Whitelist, assignment, Lane Calibration và Parking Layout.
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

## 5. Lane Calibration

Lane Calibration sử dụng danh sách **Vehicles**, không còn Target Pairs người–xe.

Luồng cấu hình Vehicle:

1. Scan một TID đã có trong RFID Tag Whitelist.
2. Nếu TID đang gắn active với Vehicle, hệ thống tự hiển thị License Plate và Owner hiện tại.
3. Nếu TID chưa được gán, người vận hành có thể Quick Create Vehicle chỉ bằng License Plate để test; khi lưu, TID được gán active cho Vehicle đó.
4. Nếu Vehicle chưa có Owner, người vận hành có thể Quick Create hoặc chọn `nsp.user` ngay trong dòng Vehicle.
5. Nếu Vehicle đã có Owner, Owner chỉ hiển thị và không được thay đổi từ Lane Calibration.

Lane Calibration lắp ráp các danh tính thiết bị độc lập theo ngữ cảnh phiên đo:

- Server.
- Controller.
- RFID Reader.
- Antenna Port Mapping: `No. 1 → Antenna`, `No. 2 → Antenna`, ...

Quan hệ Server–Controller–Reader–Antenna chỉ tồn tại trong Lane Calibration được phát hành; không ghi thành cây thiết bị cố định trong Device Whitelist.

Measurement điều chỉnh tạm thời:

- Reader Power (dBm).
- Read Interval (ms).
- TID Start Address và TID Length theo WORD 16-bit.
- Antenna được ánh xạ vào từng cổng Reader.

Cloud chỉ đồng bộ Lane Calibration đang `ready` hoặc `running` xuống đúng Edge Server được chọn trong assembly. Payload chỉ chứa Vehicles và các thiết bị đang được sử dụng trong assembly phát hành.

## 6. Parking Layout

Parking Layout lắp ráp thiết bị độc lập thành cấu hình vận hành thực tế:

- Parking Area → Lane.
- Lane chọn Server và Controller theo ngữ cảnh vận hành.
- Reader và Antenna Port Mapping được cấu hình cho Lane.
- Directed Antenna Movement Rule gồm From Reader/Antenna, To Reader/Antenna, `event_type` và `duration_seconds`.
- Không dùng `direction`; `event_type` là `check_in` hoặc `check_out`.

Reader technical configuration gửi xuống Controller gồm:

- Power.
- Read Interval.
- TID Start Address (Words).
- TID Length (Words).
- Antenna Port Mapping.

Parking Layout chỉ trình bày danh sách Lanes; Movement Rules nằm ngay trong từng Lane. `Motorbike Capacity` đã bỏ. Số cột Live Monitor được điều chỉnh trực tiếp trên Live Monitor và lưu theo trình duyệt.

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

RFID Tag Whitelist/assignment tiếp tục đồng bộ qua snapshot nghiệp vụ RFID.

Đối với hạ tầng Gatekeeper, Cloud **không đồng bộ toàn bộ Device Whitelist** và không đồng bộ một cây Server → Controller → Reader → Antenna cố định. Cloud chỉ đồng bộ projection của cấu hình đang được phát hành:

- Lane Calibration `ready`/`running`.
- Parking Layout đã Publish.

Mỗi payload chứa đúng Technical Code, Serial Number nếu có, quan hệ assembly, Reader Parameters, Antenna Port Mapping, Vehicles hoặc Lanes/Movement Rules cần thiết cho cấu hình đó. Thiết bị chỉ có trong Device Whitelist nhưng không tham gia cấu hình phát hành sẽ không được gửi xuống Edge.

## 10. Device Whitelist và UI vận hành

`Device Whitelist` chỉ quản lý danh tính độc lập của thiết bị:

- Device Type: Server, Controller, RFID Reader, Antenna.
- Management Code/Technical Code tự sinh.
- Serial Number nếu có; RFID Reader bắt buộc Serial Number.
- Photo.
- Active.

Device Whitelist không lưu thiết bị cha, cổng Reader, mục đích sử dụng hoặc quan hệ vận hành. Server, Controller, RFID Reader và Antenna không phụ thuộc nhau tại master data.

Các màn hình `Lane Calibration` và `Parking Layout` chỉ được chọn thiết bị active đã tồn tại trong Device Whitelist; không Quick Create hoặc Create and Edit thiết bị. Quan hệ giữa các thiết bị được lưu trong từng assembly đo đạc/vận hành và có thể thay đổi ở cấu hình khác mà không làm thay đổi danh tính thiết bị.
