# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class NspParkingTransactionNotificationExt(models.Model):
    _inherit = "nsp.parking.transaction"

    notification_ids = fields.One2many("nsp.notification", "parking_transaction_id", string="Notifications", readonly=True)
    notification_count = fields.Integer(string="Notifications", compute="_compute_notification_count")

    def _compute_notification_count(self):
        for rec in self:
            rec.notification_count = len(rec.notification_ids)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._create_parking_notifications()
        return records

    def _notification_target_user(self):
        self.ensure_one()
        if self.vehicle_id and self.vehicle_id.owner_id:
            return self.vehicle_id.owner_id
        if self.user_card_id:
            user_card = self.env["nsp.user.card"].sudo().search([
                ("card_id", "=", self.user_card_id.id),
                ("state", "=", "active"),
            ], limit=1)
            if user_card:
                return user_card.user_id
        return self.env["nsp.user"].browse()

    def _parking_notification_values(self):
        self.ensure_one()
        direction_label = _("entered") if self.direction == "entry" else _("exited")
        vn_direction = _("vào") if self.direction == "entry" else _("ra")
        vehicle = self.license_plate or self.vehicle_display or self.vehicle_tid or _("Unknown vehicle")
        gate = self.gate_display or self.gate_code or _("Unknown gate")
        denied = self.status == "denied"
        title = _("Vehicle %s: %s") % (vn_direction, vehicle)
        if denied:
            title = _("Denied vehicle %s: %s") % (vn_direction, vehicle)
        message = _("Vehicle %s %s at %s.") % (vehicle, direction_label, gate)
        if denied and self.error_message:
            message = "%s %s" % (message, self.error_message)
        target_user = self._notification_target_user()
        recipient = target_user.notification_user_id if target_user and hasattr(target_user, "notification_user_id") and target_user.notification_user_id and target_user.notification_enabled else self.env["res.users"].browse()
        ntype = "parking_denied" if denied else ("parking_entry" if self.direction == "entry" else "parking_exit")
        return {
            "name": title,
            "message": message,
            "notification_type": ntype,
            "monitor_channel": "parking_monitor",
            "severity": "warning" if denied else "info",
            "event_time": self.time_entered or fields.Datetime.now(),
            "target_user_id": target_user.id if target_user else False,
            "recipient_user_id": recipient.id if recipient else False,
            "parking_transaction_id": self.id,
            "vehicle_id": self.vehicle_id.id if self.vehicle_id else False,
            "gate_id": self.gate_id.id if self.gate_id else False,
            "controller_id": self.controller_id.id if self.controller_id else False,
            "source_model": self._name,
            "source_record_id": self.id,
            "dedupe_key": "parking:%s" % self.transaction_uid if self.transaction_uid else "parking-id:%s" % self.id,
        }

    def _create_parking_notifications(self):
        Notification = self.env["nsp.notification"].sudo()
        for tx in self:
            if Notification.search_count([("parking_transaction_id", "=", tx.id)]):
                continue
            Notification.create(tx._parking_notification_values())
        return True
