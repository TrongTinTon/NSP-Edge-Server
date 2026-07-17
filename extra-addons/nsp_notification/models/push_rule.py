# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class NspPushRule(models.Model):
    _name = "nsp.push.rule"
    _description = "NSP Push Rule"
    _order = "sequence, name, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, index=True)
    notification_type = fields.Selection([
        ("all", "All Types"),
        ("parking_entry", "Vehicle Entry"),
        ("parking_exit", "Vehicle Exit"),
        ("parking_denied", "Parking Denied"),
        ("system_alert", "System Alert"),
        ("emergency", "Emergency"),
    ], default="all", required=True, index=True)
    min_severity = fields.Selection([
        ("info", "Info"),
        ("warning", "Warning"),
        ("critical", "Critical"),
    ], default="info", required=True, index=True)
    branch_id = fields.Many2one("nsp.branch", string="Branch", ondelete="cascade", index=True, help="Leave empty to apply to all branches.")
    recipient_scope = fields.Selection([
        ("target_user", "NSP Target User"),
        ("odoo_recipient", "Odoo Recipient"),
        ("target_and_recipient", "Target + Odoo Recipient"),
        ("manual_users", "Selected Odoo Users"),
        ("all_internal_users", "All Internal Users"),
    ], default="target_user", required=True)
    manual_user_ids = fields.Many2many("res.users", "nsp_push_rule_res_users_rel", "rule_id", "user_id", string="Selected Odoo Users")
    provider_type = fields.Selection([
        ("any", "Any Active Provider"),
        ("fcm", "FCM"),
        ("apns", "APNs"),
        ("hms", "HMS"),
        ("in_app", "In-App"),
    ], default="any", required=True)
    priority = fields.Selection([
        ("normal", "Normal"),
        ("high", "High"),
        ("emergency", "Emergency"),
    ], default="normal", required=True)
    note = fields.Text()

    _severity_rank = {"info": 1, "warning": 2, "critical": 3}

    def _matches_notification(self, notification):
        self.ensure_one()
        if self.notification_type != "all" and self.notification_type != notification.notification_type:
            return False
        if self.branch_id and notification.branch_id and self.branch_id != notification.branch_id:
            return False
        if self.branch_id and not notification.branch_id:
            return False
        return self._severity_rank.get(notification.severity or "info", 1) >= self._severity_rank.get(self.min_severity or "info", 1)

    @api.model
    def _rules_for_notification(self, notification):
        rules = self.sudo().search([("active", "=", True)])
        return rules.filtered(lambda rule: rule._matches_notification(notification))

    @api.model
    def _devices_for_notification(self, notification):
        Device = self.env["nsp.push.device"].sudo()
        devices = Device.browse()
        for rule in self._rules_for_notification(notification):
            nsp_users = self.env["nsp.user"].sudo().browse()
            odoo_users = self.env["res.users"].sudo().browse()
            if rule.recipient_scope in ("target_user", "target_and_recipient") and notification.target_user_id:
                nsp_users |= notification.target_user_id
            if rule.recipient_scope in ("odoo_recipient", "target_and_recipient") and notification.recipient_user_id:
                odoo_users |= notification.recipient_user_id
            if rule.recipient_scope == "manual_users":
                odoo_users |= rule.manual_user_ids
            if rule.recipient_scope == "all_internal_users":
                odoo_users |= self.env["res.users"].sudo().search([("active", "=", True), ("share", "=", False)])

            domain = [("status", "=", "active"), ("notification_enabled", "=", True)]
            if rule.provider_type != "any":
                domain.append(("provider_type", "=", rule.provider_type))
            scoped_domain = []
            if nsp_users:
                scoped_domain.append(("user_id", "in", nsp_users.ids))
            if odoo_users:
                scoped_domain.append(("odoo_user_id", "in", odoo_users.ids))
                linked_nsp_users = self.env["nsp.user"].sudo().search([("notification_user_id", "in", odoo_users.ids), ("notification_enabled", "=", True)])
                if linked_nsp_users:
                    scoped_domain.append(("user_id", "in", linked_nsp_users.ids))
            if not scoped_domain:
                continue
            if len(scoped_domain) == 1:
                devices |= Device.search(domain + scoped_domain)
            else:
                # OR all recipient-specific domains while keeping common filters.
                or_domain = ["|"] * (len(scoped_domain) - 1) + scoped_domain
                devices |= Device.search(domain + or_domain)
        return devices
