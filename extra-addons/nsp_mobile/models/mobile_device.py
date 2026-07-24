# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


TOUCH_INTERVAL = timedelta(seconds=60)


class NspMobileDevice(models.Model):
    _name = "nsp.mobile.device"
    _description = "NSP Mobile Device"
    _order = "last_seen_at desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    user_id = fields.Many2one("nsp.user", required=True, index=True, ondelete="cascade")
    device_uid = fields.Char(required=True, copy=False, readonly=True, index=True)
    platform = fields.Selection([
        ("android", "Android"), ("ios", "iOS"), ("web", "Web"), ("other", "Other"),
    ], required=True, default="other", index=True)
    device_name = fields.Char()
    app_version = fields.Char()
    push_provider = fields.Selection([
        ("none", "None"),
        ("fcm", "Firebase Cloud Messaging"),
        ("apns", "Apple Push Notification Service"),
        ("custom", "Custom"),
    ], required=True, default="none", index=True)
    push_token = fields.Char(copy=False, groups="nsp_core.group_nsp_it_parking,base.group_system")
    push_enabled = fields.Boolean(default=False, index=True)
    active = fields.Boolean(default=True, index=True)
    last_seen_at = fields.Datetime(readonly=True, index=True)
    last_sync_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("device_uid_unique", "unique(device_uid)", "Mobile Device UID must be unique."),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_mobile_device_user_active_seen_idx
                ON nsp_mobile_device (user_id, active, last_seen_at DESC)
            """
        )

    @api.depends("device_name", "platform", "device_uid", "user_id.name")
    def _compute_display_name(self):
        selection = dict(self._fields["platform"].selection)
        for rec in self:
            label = rec.device_name or selection.get(rec.platform) or _("Device")
            rec.display_name = "%s — %s" % (rec.user_id.name or _("User"), label)

    @api.constrains("push_provider", "push_token", "push_enabled")
    def _check_push_configuration(self):
        for rec in self:
            if rec.push_enabled and rec.push_provider == "none":
                raise ValidationError(_("Push Provider is required when Push is enabled."))
            if rec.push_enabled and not rec.push_token:
                raise ValidationError(_("Push Token is required when Push is enabled."))

    @api.model
    def register_or_update(self, user, payload):
        device_uid = str(payload.get("device_uid") or "").strip()
        if not device_uid:
            raise ValidationError(_("device_uid is required."))
        platform = str(payload.get("platform") or "other").strip().lower()
        if platform not in dict(self._fields["platform"].selection):
            platform = "other"
        provider = str(payload.get("push_provider") or "none").strip().lower()
        if provider not in dict(self._fields["push_provider"].selection):
            provider = "custom"
        push_token = str(payload.get("push_token") or "").strip() or False
        push_enabled = bool(payload.get("push_enabled")) and provider != "none" and bool(push_token)
        vals = {
            "user_id": user.id,
            "platform": platform,
            "device_name": str(payload.get("device_name") or "").strip() or False,
            "app_version": str(payload.get("app_version") or "").strip() or False,
            "push_provider": provider,
            "push_token": push_token,
            "push_enabled": push_enabled,
            "active": True,
            "last_seen_at": fields.Datetime.now(),
        }
        rec = self.sudo().search([("device_uid", "=", device_uid)], limit=1)
        if rec:
            if rec.user_id != user and "nsp.mobile.session" in self.env.registry.models:
                old_sessions = self.env["nsp.mobile.session"].sudo().search([
                    ("device_id", "=", rec.id),
                    ("state", "=", "active"),
                ])
                if old_sessions:
                    old_sessions.revoke()
            changed = {key: value for key, value in vals.items() if rec[key] != value}
            if changed:
                rec.sudo().write(changed)
            return rec
        vals["device_uid"] = device_uid
        return self.sudo().create(vals)

    def write(self, vals):
        result = super().write(vals)
        if vals.get("active") is False and "nsp.mobile.session" in self.env.registry.models:
            sessions = self.env["nsp.mobile.session"].sudo().search([
                ("device_id", "in", self.ids),
                ("state", "=", "active"),
            ])
            if sessions:
                sessions.revoke()
        return result

    def touch(self, sync=False, force=False):
        now = fields.Datetime.now()
        stale = self.filtered(
            lambda rec: force
            or sync
            or not rec.last_seen_at
            or now - rec.last_seen_at >= TOUCH_INTERVAL
        )
        if stale:
            vals = {"last_seen_at": now}
            if sync:
                vals["last_sync_at"] = now
            stale.sudo().write(vals)
        return True
