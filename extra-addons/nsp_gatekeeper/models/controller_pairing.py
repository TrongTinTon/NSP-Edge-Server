# -*- coding: utf-8 -*-
"""Controller pairing and per-controller credentials under a shared Application."""
import hashlib
import re
import secrets
import uuid
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.t4_coreapi.models.core_api_application import SECRET_CRYPT_CONTEXT


PAIRING_ACTIVE_STATES = ("pending", "approved")


class NspControllerApiCredential(models.Model):
    _name = "nsp.controller.api.credential"
    _description = "NSP Controller API Credential"
    _order = "controller_id, id desc"

    controller_id = fields.Many2one(
        "nsp.controller", required=True, ondelete="cascade", index=True,
        domain="[('node_type', '=', 'controller')]",
    )
    application_id = fields.Many2one(
        "core.api.application", required=True, ondelete="cascade", index=True,
    )
    client_id = fields.Char(required=True, readonly=True, copy=False, index=True)
    client_secret_hash = fields.Char(
        required=True, readonly=True, copy=False, groups="base.group_system",
    )
    state = fields.Selection(
        [("active", "Active"), ("revoked", "Revoked")],
        required=True, default="active", index=True,
    )
    last_auth_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        ("nsp_controller_credential_client_id_unique", "unique(client_id)", "Controller credential Client ID must be unique."),
    ]

    def init(self):
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_controller_api_credential_active_uniq
                ON nsp_controller_api_credential (controller_id)
             WHERE state = 'active'
        """)

    @api.model
    def _new_client_id(self, controller):
        slug = re.sub(r"[^a-z0-9]+", "_", (controller.controller_id or "controller").lower()).strip("_")[:28]
        return "gk_%s_%s" % (slug or "controller", secrets.token_hex(4))

    @api.model
    def issue_for_controller(self, controller, application):
        controller.ensure_one()
        application.ensure_one()
        if controller.node_type != "controller":
            raise ValidationError(_("Pairing credentials can only be issued to a Controller."))
        if application.state != "active":
            raise ValidationError(_("The shared Core API Application is not active."))

        # A successful re-pair must not leave two live credentials for the same Controller.
        self.sudo().search([
            ("controller_id", "=", controller.id),
            ("state", "=", "active"),
        ]).write({"state": "revoked"})

        plaintext = secrets.token_urlsafe(40)
        credential = self.sudo().create({
            "controller_id": controller.id,
            "application_id": application.id,
            "client_id": self._new_client_id(controller),
            "client_secret_hash": SECRET_CRYPT_CONTEXT.hash(plaintext),
            "state": "active",
        })
        return credential, plaintext

    def verify_secret(self, plaintext):
        self.ensure_one()
        return bool(
            self.state == "active"
            and plaintext
            and self.client_secret_hash
            and SECRET_CRYPT_CONTEXT.verify(plaintext, self.client_secret_hash)
        )


class NspControllerPairingRequest(models.Model):
    _name = "nsp.controller.pairing.request"
    _description = "Controller Pairing Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "requested_at desc, id desc"
    _rec_name = "pairing_request_uid"

    pairing_request_uid = fields.Char(
        string="Pairing Request UID", required=True, readonly=True, copy=False,
        default=lambda self: "PAIR-%s" % uuid.uuid4().hex.upper(), index=True,
    )
    edge_server_id = fields.Many2one(
        "nsp.controller", string="Edge Server", required=True, ondelete="restrict", index=True,
        domain="[('node_type', '=', 'edge_server')]", tracking=True,
    )
    edge_server_code = fields.Char(
        related="edge_server_id.controller_id", string="Edge Server Code",
        store=True, readonly=True, index=True,
    )
    machine_id = fields.Char(required=True, readonly=True, copy=False, index=True)
    machine_name = fields.Char(readonly=True, copy=False)
    software_version = fields.Char(readonly=True, copy=False)
    pairing_status = fields.Selection([
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("delivered", "Delivered"),
        ("rejected", "Rejected"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
    ], required=True, default="pending", index=True, tracking=True)
    controller_id = fields.Many2one(
        "nsp.controller", string="Controller", ondelete="restrict", index=True,
        domain="[('node_type', '=', 'controller')]", tracking=True,
    )
    controller_code = fields.Char(
        related="controller_id.controller_id", string="Controller Code",
        store=True, readonly=True, index=True,
    )
    requested_at = fields.Datetime(required=True, readonly=True, default=fields.Datetime.now, index=True)
    expires_at = fields.Datetime(required=True, readonly=True, index=True)
    approved_at = fields.Datetime(readonly=True, copy=False)
    delivered_at = fields.Datetime(readonly=True, copy=False)
    rejection_reason_code = fields.Selection([
        ("unknown_machine", "Unknown Machine"),
        ("wrong_edge_server", "Wrong Edge Server"),
        ("controller_not_available", "Controller Not Available"),
        ("duplicate_request", "Duplicate Request"),
    ], readonly=True, copy=False)

    pairing_token_hash = fields.Char(readonly=True, copy=False, groups="base.group_system")
    credential_id = fields.Many2one(
        "nsp.controller.api.credential", readonly=True, copy=False,
        ondelete="set null", groups="base.group_system",
    )
    credential_secret_encrypted = fields.Text(readonly=True, copy=False, groups="base.group_system")

    _sql_constraints = [
        ("nsp_pairing_request_uid_unique", "unique(pairing_request_uid)", "Pairing Request UID must be unique."),
    ]

    def init(self):
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_pairing_active_machine_uniq
                ON nsp_controller_pairing_request (edge_server_id, machine_id)
             WHERE pairing_status IN ('pending', 'approved')
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS nsp_pairing_approved_controller_uniq
                ON nsp_controller_pairing_request (controller_id)
             WHERE controller_id IS NOT NULL AND pairing_status IN ('approved', 'delivered')
        """)

    @api.model
    def _pairing_ttl(self):
        value = self.env["ir.config_parameter"].sudo().get_param(
            "nsp_gatekeeper.controller_pairing_ttl_minutes", "30"
        )
        try:
            minutes = int(value or 30)
        except Exception:
            minutes = 30
        return max(5, min(minutes, 1440))

    @api.model
    def _fernet(self):
        params = self.env["ir.config_parameter"].sudo()
        key = params.get_param("nsp_gatekeeper.controller_pairing_fernet_key")
        if not key:
            key = Fernet.generate_key().decode("ascii")
            params.set_param("nsp_gatekeeper.controller_pairing_fernet_key", key)
        return Fernet(key.encode("ascii"))

    @api.model
    def _encrypt_secret(self, plaintext):
        return self._fernet().encrypt((plaintext or "").encode("utf-8")).decode("ascii")

    def _decrypt_secret(self):
        self.ensure_one()
        if not self.credential_secret_encrypted:
            return False
        try:
            return self._fernet().decrypt(self.credential_secret_encrypted.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return False

    @api.model
    def _hash_pairing_token(self, token):
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def check_pairing_token(self, token):
        self.ensure_one()
        expected = self.pairing_token_hash or ""
        actual = self._hash_pairing_token(token)
        return bool(token and expected and secrets.compare_digest(expected, actual))

    @api.model
    def _edge_server_identity(self):
        """Resolve the Edge Server from active NSP Sync Authentication.

        Cloud does not install ``nsp_sync``. Therefore the presence of exactly
        one active authentication bound to one Edge Server is the runtime
        marker that this database is an Edge Server and may accept public
        Controller pairing requests.
        """
        if "nsp.sync.auth" not in self.env.registry.models:
            raise ValidationError(_("Controller Pairing Requests are accepted only by an Edge Server with NSP Sync installed."))
        authentications = self.env["nsp.sync.auth"].sudo().search([
            ("active", "=", True),
            ("edge_server_id", "!=", False),
        ])
        edge_servers = authentications.mapped("edge_server_id").exists()
        # Multiple Cloud credentials are allowed only when all of them belong to
        # the same Edge Server identity. Pairing must never guess between nodes.
        if len(edge_servers) != 1:
            return self.env["nsp.controller"].browse()
        edge_server = edge_servers[0]
        if not edge_server.active or edge_server.node_type != "edge_server":
            return self.env["nsp.controller"].browse()
        return edge_server

    def sync_payload(self):
        self.ensure_one()
        payload = {
            "pairing_request_uid": self.pairing_request_uid,
            "machine_id": self.machine_id,
            "pairing_status": self.pairing_status,
            "requested_at": fields.Datetime.to_string(self.requested_at),
            "expires_at": fields.Datetime.to_string(self.expires_at),
        }
        if self.machine_name:
            payload["machine_name"] = self.machine_name
        if self.software_version:
            payload["software_version"] = self.software_version
        if self.pairing_status == "delivered":
            payload.update({
                "controller_code": self.controller_code,
                "delivered_at": fields.Datetime.to_string(self.delivered_at),
            })
        return payload

    def _mark_sync_pending(self, message=False):
        self.ensure_one()
        try:
            Record = self.env["nsp.sync.record"].sudo()
        except Exception:
            return False
        try:
            return Record.mark_pending(
                controller=self.edge_server_id,
                action_code="nsp_controller_pairing_requests_sync",
                action_name="NSP Controller Pairing Requests Sync",
                record=self,
                record_key=self.pairing_request_uid,
                message=message or _("Controller pairing state changed."),
                operation="push",
            )
        except Exception:
            return False

    @api.model
    def create_public_request(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        allowed = {"machine_id", "machine_name", "software_version"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValidationError(_("Unsupported field(s): %s") % ", ".join(unknown))
        machine_id = str(payload.get("machine_id") or "").strip()
        if not machine_id:
            raise ValidationError(_("machine_id is required."))
        if len(machine_id) > 160:
            raise ValidationError(_("machine_id must not exceed 160 characters."))
        edge_server = self._edge_server_identity()
        if not edge_server:
            raise ValidationError(_("Exactly one active Edge Server must be configured on this server."))
        paired_controller = self.env["nsp.controller"].sudo().search([
            ("node_type", "=", "controller"),
            ("parent_id", "=", edge_server.id),
            ("paired_machine_id", "=", machine_id),
            ("active", "=", True),
        ], limit=1)
        if paired_controller:
            raise ValidationError(_("This machine is already paired with a Controller."))

        active_request = self.sudo().search([
            ("edge_server_id", "=", edge_server.id),
            ("machine_id", "=", machine_id),
            ("pairing_status", "in", list(PAIRING_ACTIVE_STATES)),
            ("expires_at", ">", fields.Datetime.now()),
        ], limit=1)
        if active_request:
            raise ValidationError(_("An active pairing request already exists for this machine."))

        token = secrets.token_urlsafe(32)
        record = self.sudo().create({
            "edge_server_id": edge_server.id,
            "machine_id": machine_id[:160],
            "machine_name": str(payload.get("machine_name") or "").strip()[:160] or False,
            "software_version": str(payload.get("software_version") or "").strip()[:64] or False,
            "pairing_token_hash": self._hash_pairing_token(token),
            "expires_at": fields.Datetime.now() + timedelta(minutes=self._pairing_ttl()),
        })
        record._mark_sync_pending(_("New Controller pairing request."))
        return record, token

    def _ensure_pending(self):
        self.ensure_one()
        self.expire_if_needed()
        if self.pairing_status != "pending":
            raise UserError(_("Only pending pairing requests can be approved or rejected."))

    def expire_if_needed(self):
        self.ensure_one()
        if self.pairing_status in PAIRING_ACTIVE_STATES and self.expires_at and self.expires_at < fields.Datetime.now():
            self.sudo().write({
                "pairing_status": "expired",
                "credential_secret_encrypted": False,
            })
        return self

    def _validate_controller_scope(self, controller):
        self.ensure_one()
        controller.ensure_one()
        if controller.node_type != "controller":
            raise ValidationError(_("The selected record is not a Controller."))
        if controller.parent_id != self.edge_server_id:
            raise ValidationError(_("The Controller is not assigned to this Edge Server."))
        if not controller.active or controller.status in ("block", "revoked"):
            raise ValidationError(_("The Controller is not available for pairing."))
        if controller.paired_machine_id and controller.paired_machine_id != self.machine_id:
            raise ValidationError(_("The Controller is already paired with another machine."))
        other = self.sudo().search([
            ("id", "!=", self.id),
            ("controller_id", "=", controller.id),
            ("pairing_status", "in", ["approved", "delivered"]),
        ], limit=1)
        if other:
            raise ValidationError(_("The Controller already has an approved pairing request."))

    def action_approve(self):
        for record in self:
            record._ensure_pending()
            if not record.controller_id:
                raise UserError(_("Select an existing Controller before approval."))
            record._validate_controller_scope(record.controller_id)
            record.write({
                "pairing_status": "approved",
                "approved_at": fields.Datetime.now(),
                "rejection_reason_code": False,
            })
        return True

    def action_reject(self):
        for record in self:
            record._ensure_pending()
            record.write({
                "pairing_status": "rejected",
                "credential_secret_encrypted": False,
            })
        return True

    def action_cancel(self):
        for record in self:
            record.expire_if_needed()
            if record.pairing_status not in PAIRING_ACTIVE_STATES:
                raise UserError(_("Only pending or approved pairing requests can be cancelled."))
            if record.credential_id:
                record.credential_id.write({"state": "revoked"})
            record.write({
                "pairing_status": "cancelled",
                "credential_secret_encrypted": False,
            })
            record._mark_sync_pending(_("Controller pairing request cancelled."))
        return True

    def action_open_controller(self):
        self.ensure_one()
        if not self.controller_id:
            raise UserError(_("No Controller is assigned."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Controller"),
            "res_model": "nsp.controller",
            "res_id": self.controller_id.id,
            "view_mode": "form",
            "target": "current",
        }

    @api.model
    def apply_cloud_decisions(self, edge_server, items):
        edge_server.ensure_one()
        if edge_server.node_type != "edge_server":
            raise ValidationError(_("Edge Server is required."))
        results = []
        for index, item in enumerate(items or []):
            uid = str((item or {}).get("pairing_request_uid") or "").strip() if isinstance(item, dict) else ""
            try:
                if not isinstance(item, dict) or not uid:
                    raise ValidationError(_("Invalid pairing decision payload."))
                pairing = self.sudo().search([
                    ("pairing_request_uid", "=", uid),
                    ("edge_server_id", "=", edge_server.id),
                ], limit=1)
                if not pairing:
                    raise ValidationError(_("Pairing request was not found on this Edge Server."))
                pairing.apply_cloud_decision(item)
                results.append({"index": index, "record_key": uid, "status": "processed"})
            except Exception as exc:
                results.append({"index": index, "record_key": uid, "status": "rejected", "message": str(exc)})
        return results

    def apply_cloud_decision(self, values):
        """Apply one Cloud decision on the Edge Server.

        The Local sync worker calls this method after pulling
        /controller-pairing-decisions/sync. Credentials are generated locally;
        no secret crosses the Cloud-to-Local synchronization channel.
        """
        self.ensure_one()
        status = str((values or {}).get("pairing_status") or "").strip()
        if status in ("rejected", "cancelled", "expired"):
            if self.credential_id:
                self.credential_id.write({"state": "revoked"})
            self.write({
                "pairing_status": status,
                "credential_secret_encrypted": False,
                "rejection_reason_code": (values or {}).get("reason_code") or False,
            })
            return self
        if status != "approved":
            raise ValidationError(_("Unsupported pairing decision."))

        controller_code = str((values or {}).get("controller_code") or "").strip()
        if self.pairing_status == "delivered" and self.controller_code == controller_code:
            return self
        if (
            self.pairing_status == "approved"
            and self.controller_code == controller_code
            and self.credential_id
            and self.credential_id.state == "active"
            and self.credential_secret_encrypted
        ):
            return self
        controller = self.env["nsp.controller"].sudo().search([
            ("controller_id", "=", controller_code),
            ("node_type", "=", "controller"),
            ("parent_id", "=", self.edge_server_id.id),
        ], limit=1)
        if not controller:
            raise ValidationError(_("The approved Controller is not assigned to this Edge Server."))
        self._validate_controller_scope(controller)
        application = self.edge_server_id.core_api_application_id
        if not application:
            raise ValidationError(_("The Edge Server has no shared Controller Core API Application."))
        credential, plaintext = self.env["nsp.controller.api.credential"].sudo().issue_for_controller(
            controller, application
        )
        self.write({
            "controller_id": controller.id,
            "pairing_status": "approved",
            "approved_at": fields.Datetime.to_datetime((values or {}).get("approved_at")) if (values or {}).get("approved_at") else fields.Datetime.now(),
            "credential_id": credential.id,
            "credential_secret_encrypted": self._encrypt_secret(plaintext),
        })
        return self

    def delivery_payload_once(self):
        self.ensure_one()
        self.expire_if_needed()
        if self.pairing_status == "delivered":
            return {"pairing_status": "delivered"}
        if self.pairing_status != "approved":
            return {"pairing_status": self.pairing_status}
        if not self.credential_id or self.credential_id.state != "active":
            return {"pairing_status": "approved"}
        plaintext = self._decrypt_secret()
        if not plaintext:
            raise UserError(_("Pairing credential is not available for delivery."))

        payload = {
            "pairing_status": "approved",
            "controller_code": self.controller_code,
            "edge_server_code": self.edge_server_code,
            "service_code": self.credential_id.application_id.service_code,
            "token_endpoint": "/auth/token",
            "client_id": self.credential_id.client_id,
            "client_secret": plaintext,
        }
        # Do not destroy the encrypted secret before the Controller proves that
        # it received it by authenticating at /auth/token. An HTTP response can be
        # lost after the database transaction commits; marking delivered here would
        # permanently strand the Controller without credentials.
        if not self.delivered_at:
            self.write({"delivered_at": fields.Datetime.now()})
        return payload

    @api.model
    def cron_expire_pairing_requests(self):
        expired = self.sudo().search([
            ("pairing_status", "in", list(PAIRING_ACTIVE_STATES)),
            ("expires_at", "<", fields.Datetime.now()),
        ])
        if expired:
            expired.mapped("credential_id").filtered(lambda c: c.state == "active").write({"state": "revoked"})
            expired.write({
                "pairing_status": "expired",
                "credential_secret_encrypted": False,
            })
            for record in expired:
                record._mark_sync_pending(_("Controller pairing request expired."))
        return True


class CoreApiApplicationControllerCredential(models.Model):
    _inherit = "core.api.application"

    nsp_controller_credential_ids = fields.One2many(
        "nsp.controller.api.credential", "application_id",
        string="Controller Credentials", readonly=True,
    )

    @api.model
    def authenticate_client_with_reason(self, client_id, client_secret, ip_address=None):
        application, reason = super().authenticate_client_with_reason(
            client_id, client_secret, ip_address=ip_address
        )
        if application:
            return application, reason
        credential = self.env["nsp.controller.api.credential"].sudo().search([
            ("client_id", "=", str(client_id or "").strip()),
            ("state", "=", "active"),
            ("application_id.state", "=", "active"),
        ], limit=1)
        if not credential:
            return application, reason
        credential.application_id.check_ip_allowed(ip_address)
        credential.application_id.check_auth_rate_limit()
        if not credential.verify_secret(client_secret):
            return self.browse(), _("Invalid client credentials.")
        now = fields.Datetime.now()
        credential.write({"last_auth_at": now})
        pairing = self.env["nsp.controller.pairing.request"].sudo().search([
            ("credential_id", "=", credential.id),
            ("pairing_status", "=", "approved"),
        ], limit=1)
        if pairing:
            pairing.write({
                "pairing_status": "delivered",
                "delivered_at": now,
                "credential_secret_encrypted": False,
            })
            pairing.controller_id.write({
                "paired_machine_id": pairing.machine_id,
                "paired_at": now,
            })
            pairing._mark_sync_pending(_("Controller pairing credential authenticated and delivered."))
        try:
            request.update_context(nsp_controller_auth_id=credential.controller_id.id)
        except Exception:
            pass
        return credential.application_id, False


class CoreApiTokenControllerBinding(models.Model):
    _inherit = "core.api.token"

    nsp_controller_id = fields.Many2one(
        "nsp.controller", string="Bound NSP Controller", readonly=True,
        ondelete="set null", index=True,
    )

    @api.model
    def issue_for_application(self, application, revoke_existing=False):
        # New authentication must never revoke another client's active token.
        result = super().issue_for_application(application, revoke_existing=False)
        controller_id = self.env.context.get("nsp_controller_auth_id")
        if controller_id:
            result["access_token_rec"].write({"nsp_controller_id": controller_id})
            result["refresh_token_rec"].write({"nsp_controller_id": controller_id})
        return result

    @api.model
    def refresh_for_application(self, refresh_plaintext):
        application, refresh_token = self.authenticate_refresh(refresh_plaintext)
        if not application:
            return None
        self._revoke_active_tokens([("token_pair_id", "=", refresh_token.token_pair_id)])
        return self.with_context(
            nsp_controller_auth_id=refresh_token.nsp_controller_id.id if refresh_token.nsp_controller_id else False
        ).issue_for_application(application, revoke_existing=False)
