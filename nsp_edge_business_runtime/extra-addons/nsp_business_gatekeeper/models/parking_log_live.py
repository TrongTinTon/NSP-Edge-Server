# -*- coding: utf-8 -*-
from odoo import fields, models, _


class ParkingLogLiveMonitor(models.Model):
    """Presentation-only projection/broadcast behavior for Parking Logs."""

    _inherit = "nsp.parking.log"

    def _live_monitor_display_meta(self):
        self.ensure_one()
        if self.decision == "allowed":
            # Live Monitor's main grid represents Vehicle entries only. A successful
            # Check-out clears any previous alert for the Vehicle but must not create
            # another entry card. Parking Logs still retain the Check-out history.
            if self.event_type == "check_out":
                return {
                    "display_kind": "clear",
                    "display_title": "",
                    "display_reason": "",
                }
            return {
                "display_kind": "entry",
                "display_title": _("MỜI VÀO"),
                "display_reason": "",
            }
        reasons = {
            "parking_area_not_operational": _("BÃI XE TẠM NGƯNG VẬN HÀNH"),
            "missing_user_tid": _("THIẾU THẺ NGƯỜI DÙNG"),
            "user_tag_not_assigned": _("THẺ NGƯỜI DÙNG CHƯA ĐƯỢC GÁN"),
            "unauthorized_vehicle_user": _("NGƯỜI QUÉT KHÔNG ĐƯỢC PHÉP LẤY XE"),
            "vehicle_checked_in_other_area": _("XE ĐANG Ở MỘT BÃI XE KHÁC"),
        }
        return {
            "display_kind": "alert",
            "display_title": _("KHÔNG ĐƯỢC PHÉP LẤY XE")
            if self.event_type == "check_out" else _("VUI LÒNG DỪNG LẠI"),
            "display_reason": reasons.get(
                self.reason_code or "unknown",
                _("VUI LÒNG LIÊN HỆ BỘ PHẬN VẬN HÀNH"),
            ),
        }

    def _live_monitor_payload(self):
        self.ensure_one()
        area = self.parking_area_id
        vehicle = self.vehicle_id
        owner = vehicle.owner_id if vehicle else self.env["nsp.user"].browse()
        gate_user = self.user_id if self.event_type == "check_out" and self.user_id else owner
        vehicle_type = vehicle.vehicle_type_id if vehicle else self.env["nsp.vehicle.type"].browse()
        vehicle_type_code = str(vehicle_type.code or "").strip().lower() if vehicle_type else ""
        license_plate = (
            (vehicle.license_plate if vehicle else "") or self.vehicle_tid or "-"
        ).strip().upper()
        employee_name = (
            (gate_user.name or _("Unknown employee")).strip().upper()
            if gate_user else _("Unknown employee").upper()
        )
        avatar_url = ""
        if gate_user:
            avatar_field = next(
                (name for name in ("avatar_128", "image_128", "image_1920") if name in gate_user._fields),
                "",
            )
            if avatar_field:
                avatar_url = f"/web/image/nsp.user/{gate_user.id}/{avatar_field}"
        return {
            "id": self.id,
            "log_uid": self.log_uid,
            "parking_area_id": area.id if area else False,
            "lane_name": self.lane_id.name if self.lane_id else "",
            "event_type": self.event_type,
            "event_time": fields.Datetime.to_string(self.event_time) if self.event_time else "",
            "decision": self.decision,
            "vehicle_key": str(vehicle.id if vehicle else (self.vehicle_tid or self.log_uid)),
            "vehicle_type": vehicle_type_code or "other",
            "license_plate": license_plate,
            "employee_name": employee_name,
            "avatar_url": avatar_url,
            **self._live_monitor_display_meta(),
        }

    def _broadcast_live_monitor(self):
        bus = self.env["bus.bus"]
        for log in self:
            if not log.parking_area_id:
                continue
            bus._sendone(
                "broadcast",
                "nsp_parking_live_log",
                log._live_monitor_payload(),
            )
        return True
