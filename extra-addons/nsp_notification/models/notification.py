# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class NspNotification(models.Model):
    _name = "nsp.notification"
    _description = "NSP Notification"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "event_time desc, id desc"

    name = fields.Char(string="Title", required=True, tracking=True)
    message = fields.Text(string="Message", required=True, tracking=True)
    notification_type = fields.Selection([
        ("parking_entry", "Vehicle Entry"),
        ("parking_exit", "Vehicle Exit"),
        ("parking_denied", "Parking Denied"),
        ("system_alert", "System Alert"),
        ("emergency", "Emergency"),
    ], string="Type", required=True, default="system_alert", index=True, tracking=True)
    monitor_channel = fields.Selection([
        ("none", "None"),
        ("parking_monitor", "Parking Monitor"),
    ], string="Monitor Channel", required=True, default="none", index=True, tracking=True,
       help="Display/monitor delivery channel. Parking monitor notifications reference nsp.parking.transaction as the source of truth.")
    severity = fields.Selection([
        ("info", "Info"),
        ("warning", "Warning"),
        ("critical", "Critical"),
    ], string="Severity", required=True, default="info", index=True, tracking=True)
    state = fields.Selection([
        ("unread", "Unread"),
        ("read", "Read"),
        ("archived", "Archived"),
    ], string="State", required=True, default="unread", index=True, tracking=True)
    event_time = fields.Datetime(string="Event Time", default=fields.Datetime.now, required=True, index=True)

    target_user_id = fields.Many2one("nsp.user", string="NSP User", ondelete="set null", index=True)
    recipient_user_id = fields.Many2one("res.users", string="Odoo Recipient", ondelete="set null", index=True)

    parking_transaction_id = fields.Many2one("nsp.parking.transaction", string="Parking Log", ondelete="cascade", index=True)
    vehicle_id = fields.Many2one("nsp.vehicle", string="Vehicle", ondelete="set null", index=True)
    gate_id = fields.Many2one("nsp.gate", string="Gate", ondelete="set null", index=True)
    branch_id = fields.Many2one("nsp.branch", string="Branch", related="gate_id.branch_id", store=True, readonly=True, index=True)
    controller_id = fields.Many2one("nsp.controller", string="Controller", ondelete="set null", index=True)

    source_model = fields.Char(string="Source Model", readonly=True)
    source_record_id = fields.Integer(string="Source Record ID", readonly=True)
    dedupe_key = fields.Char(string="Dedupe Key", index=True, copy=False)

    read_at = fields.Datetime(string="Read At", readonly=True)
    read_by = fields.Many2one("res.users", string="Read By", readonly=True)
    active = fields.Boolean(default=True)

    push_delivery_ids = fields.One2many("nsp.push.delivery", "notification_id", string="Push Deliveries", readonly=True)
    push_delivery_count = fields.Integer(string="Push Deliveries", compute="_compute_push_delivery_stats")
    push_failed_count = fields.Integer(string="Push Failed", compute="_compute_push_delivery_stats")
    push_status = fields.Selection([
        ("none", "No Push"),
        ("queued", "Queued"),
        ("partial", "Partial"),
        ("sent", "Sent"),
        ("read", "Read"),
        ("failed", "Failed"),
    ], string="Push Status", compute="_compute_push_delivery_stats", store=False)

    _sql_constraints = [
        ("parking_transaction_unique", "unique(parking_transaction_id)", "A parking transaction can create only one notification."),
    ]

    @api.depends("push_delivery_ids.status")
    def _compute_push_delivery_stats(self):
        for rec in self:
            deliveries = rec.push_delivery_ids
            rec.push_delivery_count = len(deliveries)
            rec.push_failed_count = len(deliveries.filtered(lambda d: d.status == "failed"))
            if not deliveries:
                rec.push_status = "none"
                continue
            statuses = set(deliveries.mapped("status"))
            if statuses <= {"read"}:
                rec.push_status = "read"
            elif statuses <= {"sent", "delivered", "acked", "read"}:
                rec.push_status = "sent"
            elif "queued" in statuses or "sending" in statuses:
                rec.push_status = "queued"
            elif "failed" in statuses and len(statuses) == 1:
                rec.push_status = "failed"
            else:
                rec.push_status = "partial"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._push_realtime_bus()
            rec._push_parking_monitor_bus()
        records._create_push_deliveries_from_rules()
        if self.env["ir.config_parameter"].sudo().get_param("nsp_notification.push_send_immediate", "0") in ("1", "True", "true"):
            records.mapped("push_delivery_ids").sudo().action_send_now()
        return records

    def _push_realtime_bus(self):
        """Best-effort Odoo bus notification for user/mobile notification clients."""
        bus = self.env["bus.bus"].sudo()
        for rec in self:
            try:
                partner = rec.recipient_user_id.partner_id if rec.recipient_user_id else False
                if not partner:
                    continue
                payload = rec._mobile_payload()
                payload["type"] = "nsp_notification"
                if hasattr(bus, "_sendone"):
                    try:
                        bus._sendone(partner, "nsp_notification", payload)
                    except TypeError:
                        bus._sendone((self._cr.dbname, "res.partner", partner.id), payload)
                elif hasattr(bus, "sendone"):
                    bus.sendone((self._cr.dbname, "res.partner", partner.id), payload)
            except Exception:
                _logger.debug("Could not push NSP notification over bus", exc_info=True)
        return True

    def _is_parking_monitor_notification(self):
        self.ensure_one()
        return self.monitor_channel == "parking_monitor" and bool(self.parking_transaction_id)

    def _parking_monitor_payload(self):
        """Compact payload for parking monitor screens.

        The notification is the delivery event; the linked parking transaction
        remains the business source record. No raw controller payload is exposed.
        """
        self.ensure_one()
        tx = self.parking_transaction_id
        gate = self.gate_id or tx.gate_id
        branch = self.branch_id or (gate.branch_id if gate else self.env["nsp.branch"].browse())
        controller = self.controller_id or tx.controller_id
        vehicle = self.vehicle_id or tx.vehicle_id
        direction = tx.direction or ("exit" if self.notification_type == "parking_exit" else "entry")
        status = tx.status or ("denied" if self.notification_type == "parking_denied" else "allowed")
        direction_label = "Vào" if direction == "entry" else "Ra"
        status_label = "Được phép" if status == "allowed" else "Từ chối"
        owner = vehicle.owner_id if vehicle and getattr(vehicle, "owner_id", False) else self.env["nsp.user"].browse()
        event_time = self.event_time or tx.time_entered or fields.Datetime.now()
        return {
            "id": self.id,
            "event_id": self.id,
            "notification_id": self.id,
            "transaction_id": tx.id if tx else False,
            "source_model": "nsp.notification",
            "business_source_model": "nsp.parking.transaction",
            "event_time": fields.Datetime.to_string(event_time) if event_time else "",
            "direction": direction,
            "direction_label": direction_label,
            "status": status,
            "status_label": status_label,
            "vehicle": tx.vehicle_display or tx.license_plate or tx.vehicle_tid or (vehicle.license_plate if vehicle else "-") or "-",
            "license_plate": tx.license_plate or (vehicle.license_plate if vehicle else "") or "",
            "vehicle_tid": tx.vehicle_tid or "",
            "owner_name": owner.name if owner else "",
            "gate": tx.gate_display or tx.gate_code or (gate.name if gate else ""),
            "gate_id": gate.id if gate else False,
            "gate_code": tx.gate_code or (gate.code if gate else ""),
            "gate_name": gate.name if gate else (tx.gate_display or ""),
            "lane_id": tx.lane_id.id if getattr(tx, "lane_id", False) else False,
            "lane_code": tx.lane_code if hasattr(tx, "lane_code") else "",
            "lane_name": tx.lane_id.name if getattr(tx, "lane_id", False) else (tx.lane_display if hasattr(tx, "lane_display") else ""),
            "lane": tx.lane_display if hasattr(tx, "lane_display") else "",
            "branch_id": branch.id if branch else False,
            "branch_name": branch.name if branch else "",
            "controller_id": controller.id if controller else False,
            "controller_code": controller.controller_id if controller else "",
            "message": tx.error_message or self.message or "",
            "severity": self.severity,
        }

    def _push_parking_monitor_bus(self):
        """Best-effort realtime bus for parking monitor screens.

        Polling /api/nsp_notification/v1/parking-monitor/events remains the
        reliable source. Bus is only a low-latency hint for clients that subscribe.
        """
        bus = self.env["bus.bus"].sudo()
        for rec in self:
            try:
                if not rec._is_parking_monitor_notification():
                    continue
                payload = rec._parking_monitor_payload()
                payload["type"] = "nsp_parking_monitor"
                channels = ["nsp_parking_monitor"]
                if payload.get("gate_id"):
                    channels.append("nsp_parking_monitor_gate_%s" % payload["gate_id"])
                if payload.get("branch_id"):
                    channels.append("nsp_parking_monitor_branch_%s" % payload["branch_id"])
                for channel in channels:
                    try:
                        if hasattr(bus, "_sendone"):
                            bus._sendone(channel, "nsp_parking_monitor", payload)
                        elif hasattr(bus, "sendone"):
                            bus.sendone((self._cr.dbname, channel), payload)
                    except TypeError:
                        try:
                            bus._sendone((self._cr.dbname, channel), payload)
                        except Exception:
                            pass
            except Exception:
                _logger.debug("Could not push NSP parking monitor notification over bus", exc_info=True)
        return True

    def _mobile_payload(self):
        self.ensure_one()
        return {
            "notification_id": self.id,
            "notification_type": self.notification_type,
            "severity": self.severity,
            "title": self.name,
            "message": self.message,
            "event_time": fields.Datetime.to_string(self.event_time),
            "state": self.state,
            "vehicle_id": self.vehicle_id.id if self.vehicle_id else False,
            "license_plate": self.vehicle_id.license_plate if self.vehicle_id else False,
            "gate_id": self.gate_id.id if self.gate_id else False,
            "gate_name": self.gate_id.display_name if self.gate_id else False,
            "controller_id": self.controller_id.id if self.controller_id else False,
        }

    def _push_enabled(self):
        params = self.env["ir.config_parameter"].sudo()
        return params.get_param("nsp_notification.push_enabled", "1") in ("1", "True", "true")

    def _create_push_deliveries_from_rules(self):
        if not self._push_enabled():
            return False
        Delivery = self.env["nsp.push.delivery"].sudo()
        for rec in self.sudo():
            devices = self.env["nsp.push.rule"].sudo()._devices_for_notification(rec)
            for device in devices:
                if not device.provider_id and device.provider_type != "in_app":
                    continue
                existing = Delivery.search([("notification_id", "=", rec.id), ("device_id", "=", device.id)], limit=1)
                if existing:
                    continue
                Delivery.create({
                    "notification_id": rec.id,
                    "target_user_id": device.user_id.id if device.user_id else rec.target_user_id.id,
                    "recipient_user_id": device.odoo_user_id.id if device.odoo_user_id else rec.recipient_user_id.id,
                    "device_id": device.id,
                    "provider_id": device.provider_id.id if device.provider_id else False,
                    "provider_type": device.provider_type,
                    "priority": "emergency" if rec.notification_type == "emergency" else ("high" if rec.severity == "critical" else "normal"),
                })
        return True

    def action_prepare_push_deliveries(self):
        self._create_push_deliveries_from_rules()
        return True

    def action_send_push_now(self):
        self._create_push_deliveries_from_rules()
        self.mapped("push_delivery_ids").filtered(lambda d: d.status in ("queued", "failed")).action_send_now()
        return True

    def action_mark_read(self):
        self.write({"state": "read", "read_at": fields.Datetime.now(), "read_by": self.env.user.id})
        self.mapped("push_delivery_ids").filtered(lambda d: d.status not in ("read", "expired", "cancelled")).write({"status": "read", "read_at": fields.Datetime.now()})
        return True

    def action_mark_unread(self):
        self.write({"state": "unread", "read_at": False, "read_by": False})
        return True

    def action_archive(self):
        self.write({"state": "archived"})
        return True

    @api.model
    def create_system_alert(self, title, message, severity="critical", target_user=False, recipient_user=False, dedupe_key=False):
        """Create compact NSP system/emergency alert records for other modules.

        No raw payload is stored. Callers pass a concise title/message and optional target user.
        """
        if dedupe_key:
            existing = self.search([("dedupe_key", "=", dedupe_key), ("state", "!=", "archived")], limit=1)
            if existing:
                return existing
        vals = {
            "name": title or _("NSP System Alert"),
            "message": message or title or _("System alert"),
            "notification_type": "emergency" if severity == "critical" else "system_alert",
            "severity": severity if severity in ("info", "warning", "critical") else "warning",
            "target_user_id": target_user.id if target_user else False,
            "recipient_user_id": recipient_user.id if recipient_user else (target_user.notification_user_id.id if target_user and hasattr(target_user, "notification_user_id") and target_user.notification_user_id else False),
            "dedupe_key": dedupe_key or False,
            "source_model": "system",
            "source_record_id": 0,
        }
        return self.create(vals)

    @api.model
    def cron_create_offline_alerts(self):
        """Create compact alerts for offline Controllers/Devices without modifying Gatekeeper code."""
        now = fields.Datetime.now()
        Notification = self.sudo()
        try:
            Controller = self.env["nsp.controller"].sudo()
            for controller in Controller.search([("status", "=", "offline")], limit=200):
                key = "controller_offline:%s" % controller.id
                if Notification.search_count([("dedupe_key", "=", key), ("state", "!=", "archived")]):
                    continue
                Notification.create({
                    "name": _("Controller offline: %s") % (controller.controller_name or controller.controller_id),
                    "message": _("Controller %s is offline. Please check network, service and heartbeat.") % (controller.controller_id or controller.display_name),
                    "notification_type": "system_alert",
                    "severity": "critical",
                    "controller_id": controller.id,
                    "event_time": now,
                    "dedupe_key": key,
                    "source_model": "nsp.controller",
                    "source_record_id": controller.id,
                })
        except Exception:
            _logger.debug("NSP notification controller offline check failed", exc_info=True)
        try:
            Device = self.env["nsp.device"].sudo()
            for device in Device.search([("status", "=", "offline")], limit=500):
                key = "device_offline:%s" % device.id
                if Notification.search_count([("dedupe_key", "=", key), ("state", "!=", "archived")]):
                    continue
                Notification.create({
                    "name": _("Device offline: %s") % (device.serial_number or device.device_code),
                    "message": _("Device %s is offline. Please check power, network and controller report.") % (device.serial_number or device.device_code),
                    "notification_type": "system_alert",
                    "severity": "warning",
                    "controller_id": device.controller_id.id if device.controller_id else False,
                    "event_time": now,
                    "dedupe_key": key,
                    "source_model": "nsp.device",
                    "source_record_id": device.id,
                })
        except Exception:
            _logger.debug("NSP notification device offline check failed", exc_info=True)
        return True
