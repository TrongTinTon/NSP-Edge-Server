# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import AccessError


class NspNotification(models.Model):
    _name = "nsp.notification"
    _description = "NSP Notification"
    _order = "event_time desc, id desc"

    name = fields.Char(string="Title", required=True)
    message = fields.Text(string="Message", required=True)
    category = fields.Selection([
        ("system", "System"),
        ("parking", "Parking"),
    ], string="Category", required=True, default="system", index=True)
    severity = fields.Selection([
        ("info", "Info"),
        ("warning", "Warning"),
        ("critical", "Critical"),
    ], string="Severity", required=True, default="warning", index=True)
    state = fields.Selection([
        ("unread", "Unread"),
        ("read", "Read"),
        ("archived", "Archived"),
    ], string="State", required=True, default="unread", index=True)
    event_time = fields.Datetime(string="Event Time", required=True, default=fields.Datetime.now, index=True)

    controller_code = fields.Char(string="Controller Code", index=True, readonly=True)
    source_model = fields.Char(string="Source Model", readonly=True)
    source_record_id = fields.Integer(string="Source Record ID", readonly=True)
    recipient_user_id = fields.Many2one(
        "nsp.user", string="Recipient", readonly=True, index=True, ondelete="set null"
    )
    transaction_uid = fields.Char(string="Parking Transaction UID", readonly=True, index=True)
    parking_event_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        string="Parking Event", readonly=True, index=True,
    )
    dedupe_key = fields.Char(string="Dedupe Key", index=True, copy=False, readonly=True)

    read_at = fields.Datetime(string="Read At", readonly=True)
    read_by = fields.Many2one("res.users", string="Read By", readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("dedupe_key_unique", "unique(dedupe_key)", "Notification dedupe key must be unique."),
    ]


    _recipient_mutable_fields = frozenset({"state", "read_at", "read_by", "active"})

    def write(self, vals):
        if self.env.context.get("nsp_notification_system_write"):
            return super().write(vals)
        unsupported = sorted(set(vals) - self._recipient_mutable_fields)
        if unsupported:
            raise AccessError(_(
                "Notification event content is immutable. Unsupported field(s): %s"
            ) % ", ".join(unsupported))
        return super().write(vals)

    def unlink(self):
        if self.env.context.get("nsp_notification_system_unlink"):
            return super().unlink()
        raise AccessError(_(
            "Notifications are audit records and cannot be deleted. Archive them instead."
        ))

    @api.model
    def notify_parking_transaction(self, transaction):
        """Create one owner-facing notification from an immutable Parking Transaction.

        Notification stores only the compact business event. Raw RFID detections
        remain in Gatekeeper and are never copied into the notification layer.
        """
        transaction.ensure_one()
        vehicle = transaction.vehicle_id
        owner = vehicle.owner_id if vehicle else self.env["nsp.user"].browse()
        if not owner:
            return self.browse()

        event_type = transaction.event_type
        plate = transaction.license_plate or (vehicle.license_plate if vehicle else False) or transaction.vehicle_tid or _("Vehicle")
        lane = transaction.lane_display or _("Parking lane")
        denied = transaction.status == "denied"
        if event_type == "check_in":
            title = _("Vehicle checked in: %s") % plate
            message = _("Vehicle %(vehicle)s checked in at %(lane)s.") % {
                "vehicle": plate, "lane": lane,
            }
        else:
            title = _("Vehicle checked out: %s") % plate
            message = _("Vehicle %(vehicle)s checked out at %(lane)s.") % {
                "vehicle": plate, "lane": lane,
            }
        if denied:
            title = _("Parking denied: %s") % plate
            reason = transaction.error_message or transaction.error_code or _("Parking access denied")
            message = _("%(event)s Access was denied: %(reason)s") % {
                "event": message, "reason": reason,
            }

        dedupe_key = "parking:%s:owner:%s" % (
            transaction.transaction_uid or transaction.id,
            owner.user_code or owner.id,
        )
        existing = self.sudo().with_context(active_test=False).search(
            [("dedupe_key", "=", dedupe_key)], limit=1
        )
        if existing:
            return existing
        return self.sudo().create({
            "name": title,
            "message": message,
            "category": "parking",
            "severity": "warning" if denied else "info",
            "state": "unread",
            "event_time": transaction.event_time or fields.Datetime.now(),
            "controller_code": transaction.controller_id.controller_id if transaction.controller_id else transaction.controller_code or False,
            "source_model": transaction._name,
            "source_record_id": transaction.id,
            "recipient_user_id": owner.id,
            "transaction_uid": transaction.transaction_uid or False,
            "parking_event_type": event_type,
            "dedupe_key": dedupe_key,
            "active": True,
        })

    def action_mark_read(self):
        self.write({"state": "read", "read_at": fields.Datetime.now(), "read_by": self.env.user.id})
        return True

    def action_mark_unread(self):
        self.write({"state": "unread", "read_at": False, "read_by": False})
        return True

    def action_archive(self):
        self.write({"state": "archived", "active": False})
        return True
