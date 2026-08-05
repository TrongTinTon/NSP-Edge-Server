# -*- coding: utf-8 -*-
from datetime import timedelta
from uuid import uuid4

from odoo import api, fields, models


TOUCH_INTERVAL = timedelta(seconds=60)


class NspMobileSession(models.Model):
    _name = "nsp.mobile.session"
    _description = "NSP Mobile Session"
    _order = "create_date desc"
    _rec_name = "session_uid"

    session_uid = fields.Char(required=True, readonly=True, copy=False, index=True, default=lambda self: str(uuid4()))
    user_id = fields.Many2one("nsp.user", required=True, index=True, ondelete="cascade")
    device_id = fields.Many2one("nsp.mobile.device", required=True, index=True, ondelete="cascade")
    state = fields.Selection([("active", "Active"), ("revoked", "Revoked")], default="active", required=True, index=True)
    last_seen_at = fields.Datetime(readonly=True, index=True)
    revoked_at = fields.Datetime(readonly=True)
    last_ip = fields.Char(readonly=True)
    user_agent = fields.Char(readonly=True)

    _sql_constraints = [
        ("session_uid_unique", "unique(session_uid)", "Mobile Session UID must be unique."),
    ]

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_mobile_session_user_state_idx
                ON nsp_mobile_session (user_id, state, last_seen_at DESC)
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_mobile_session_device_state_idx
                ON nsp_mobile_session (device_id, state, last_seen_at DESC)
            """
        )

    @api.model
    def open_session(self, user, device, ip=False, user_agent=False):
        # One active login context per physical device. Re-login revokes the old session and tokens.
        old = self.sudo().search([("device_id", "=", device.id), ("state", "=", "active")])
        if old:
            old.revoke()
        return self.sudo().create({
            "user_id": user.id,
            "device_id": device.id,
            "state": "active",
            "last_seen_at": fields.Datetime.now(),
            "last_ip": ip or False,
            "user_agent": user_agent or False,
        })

    def touch(self, ip=False, force=False):
        """Throttle last-seen writes to avoid a DB write on every Mobile API request."""
        now = fields.Datetime.now()
        stale = self.filtered(
            lambda rec: force
            or not rec.last_seen_at
            or now - rec.last_seen_at >= TOUCH_INTERVAL
            or (ip and ip != rec.last_ip)
        )
        if stale:
            vals = {"last_seen_at": now}
            if ip:
                vals["last_ip"] = ip
            stale.sudo().write(vals)
            stale.mapped("device_id").touch(force=force)
        return True

    def revoke(self):
        active = self.filtered(lambda rec: rec.state == "active")
        if active:
            active.sudo().write({"state": "revoked", "revoked_at": fields.Datetime.now()})
            tokens = self.env["core.api.token"].sudo().search([
                ("token_kind", "=", "mobile"),
                ("session_uid", "in", active.mapped("session_uid")),
            ])
            if tokens:
                tokens.write({
                    "active": False,
                    "refresh_token_index": False,
                    "refresh_token_hash": False,
                    "refresh_expiration_date": False,
                })
        return True
