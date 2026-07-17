# -*- coding: utf-8 -*-
"""Local-only synchronization trace.

Cloud does not install ``nsp_sync`` and never stores Sync Jobs or Sync Records.
Each row below belongs to one Edge Server Sync Job and tracks retry/result for
one business record reconstructed from the local database.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


ACTION_NAMES = {
    "nsp_gatekeeper_branches_sync": "NSP Gatekeeper Branches Sync",
    "nsp_gatekeeper_cards_sync": "NSP Gatekeeper Cards Sync",
    "nsp_gatekeeper_employees_sync": "NSP Gatekeeper Users Sync",
    "nsp_gatekeeper_vehicles_sync": "NSP Gatekeeper Vehicles Sync",
    "nsp_gatekeeper_vehicle_borrow_sync": "NSP Gatekeeper Vehicle Borrow Sync",
    "nsp_gatekeeper_gate_config_sync": "NSP Gatekeeper Gate Config Sync",
    "nsp_gatekeeper_controller_gate_config_pull": "NSP Gatekeeper Controller Gate Config Pull",
    "nsp_gatekeeper_parking_transactions_sync": "NSP Gatekeeper Parking Transactions Sync",
    "nsp_gatekeeper_gate_measurement_sync": "NSP Gatekeeper Gate Measurement Sync",
    "nsp_controller_pairing_requests_sync": "NSP Controller Pairing Requests Sync",
    "nsp_controller_pairing_decisions_sync": "NSP Controller Pairing Decisions Sync",
}


class NspSyncRecord(models.Model):
    _name = "nsp.sync.record"
    _description = "Edge Server Sync Record"
    _order = "last_attempt_at desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    sync_job_id = fields.Many2one(
        "nsp.sync.job", string="Sync Job", required=True,
        ondelete="cascade", index=True,
    )
    sync_action_id = fields.Many2one(
        "ir.actions.core_api", string="Sync Action",
        related="sync_job_id.sync_action_id", store=True, readonly=True,
    )
    sync_action_code = fields.Char(string="Action Code", required=True, index=True)
    sync_action_name = fields.Char(string="Action Name", required=True, index=True)
    route_suffix = fields.Char(string="Route")
    record_key = fields.Char(string="Record Key", required=True, index=True)
    record_model = fields.Char(string="Record Model", index=True)
    record_id = fields.Integer(string="Record ID", index=True)
    record_display_name = fields.Char(string="Record Name")
    operation = fields.Selection([
        ("pull", "Pull"),
        ("push", "Push"),
    ], string="Operation", required=True)
    status = fields.Selection([
        ("pending", "Pending"),
        ("synced", "Synced"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),
    ], string="Status", default="pending", required=True, index=True)
    local_revision = fields.Char(string="Local Revision")
    remote_revision = fields.Char(string="Remote Revision")
    checksum = fields.Char(string="Checksum")
    last_attempt_at = fields.Datetime(string="Last Attempt At", readonly=True, index=True)
    last_synced_at = fields.Datetime(string="Last Synced At", readonly=True, index=True)
    attempt_count = fields.Integer(string="Attempts", default=0, readonly=True)
    message = fields.Text(string="Message")

    _sql_constraints = [
        (
            "nsp_sync_record_job_action_key_unique",
            "unique(sync_job_id, sync_action_code, record_key)",
            "A Sync Record already exists for this Job, Action and Record Key.",
        ),
    ]

    @api.depends("sync_job_id", "sync_action_name", "record_key")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s / %s / %s" % (
                rec.sync_job_id.display_name or "-",
                rec.sync_action_name or rec.sync_action_code or "-",
                rec.record_key or "-",
            )

    @api.model
    def _normalize_status(self, status):
        raw = str(status or "").strip().lower()
        if raw in ("success", "synced", "applied", "accepted", "ok", "done", "duplicate", "processed"):
            return "synced"
        if raw in ("failed", "fail", "error", "denied", "rejected"):
            return "failed"
        if raw in ("skipped", "skip", "ignored"):
            return "skipped"
        return "pending"

    @api.model
    def _record_identity(self, record=False, record_key=False):
        if not record:
            return False, False, record_key or False, False
        display = getattr(record, "display_name", False) or getattr(record, "name", False) or str(record.id or "")
        key = (
            record_key
            or getattr(record, "pairing_request_uid", False)
            or getattr(record, "measurement_uid", False)
            or getattr(record, "transaction_uid", False)
            or getattr(record, "code", False)
            or getattr(record, "license_plate", False)
            or getattr(record, "user_code", False)
            or display
            or str(record.id)
        )
        return record._name, record.id, key, display

    @api.model
    def _resolve_job(self, action_code, controller=False):
        code = str(action_code or "").strip()
        if not code:
            return self.env["nsp.sync.job"].browse()
        domain = [
            ("active", "=", True),
            ("sync_action_code", "=", code),
        ]
        if controller:
            edge_server = controller if controller.node_type == "edge_server" else controller.parent_id
            if edge_server:
                domain.append(("auth_id.edge_server_id", "=", edge_server.id))
        jobs = self.env["nsp.sync.job"].sudo().search(domain, limit=2)
        return jobs[:1] if len(jobs) == 1 else self.env["nsp.sync.job"].browse()

    @api.model
    def upsert_record(
        self, sync_job=False, controller=False, action_code=False, action_name=False,
        route_suffix=False, record=False, record_key=False, status="pending",
        operation="push", message=False, local_revision=False,
        remote_revision=False, checksum=False, last_synced_at=False,
    ):
        job = sync_job.exists() if sync_job else self._resolve_job(action_code, controller=controller)
        if not job:
            return self.browse()
        code = str(action_code or job.sync_action_code or "").strip()
        if not code:
            raise UserError(_("Sync Action Code is required."))
        name = action_name or ACTION_NAMES.get(code) or job.sync_action_name or code
        model, record_id, key, display = self._record_identity(record=record, record_key=record_key)
        key = str(key or "").strip()
        if not key:
            raise UserError(_("Record Key is required for Sync Record."))
        normalized_status = self._normalize_status(status)
        now = fields.Datetime.now()
        values = {
            "sync_job_id": job.id,
            "sync_action_code": code,
            "sync_action_name": name,
            "route_suffix": route_suffix or job.route_suffix or False,
            "record_key": key,
            "record_model": model or False,
            "record_id": record_id or False,
            "record_display_name": display or False,
            "operation": operation if operation in ("pull", "push") else "push",
            "status": normalized_status,
            "message": message or False,
            "local_revision": str(local_revision) if local_revision not in (False, None, "") else False,
            "remote_revision": str(remote_revision) if remote_revision not in (False, None, "") else False,
            "checksum": checksum or False,
            "last_attempt_at": now,
        }
        if last_synced_at:
            values["last_synced_at"] = last_synced_at
        elif normalized_status == "synced":
            values["last_synced_at"] = now
        existing = self.search([
            ("sync_job_id", "=", job.id),
            ("sync_action_code", "=", code),
            ("record_key", "=", key),
        ], limit=1)
        if existing:
            values["attempt_count"] = existing.attempt_count + 1
            existing.write(values)
            return existing
        values["attempt_count"] = 1
        return self.create(values)

    @api.model
    def mark_pending(self, **values):
        values["status"] = "pending"
        return self.upsert_record(**values)

    @api.model
    def mark_result(self, **values):
        values.setdefault("status", "synced")
        return self.upsert_record(**values)

    def action_mark_synced(self):
        self.write({
            "status": "synced",
            "last_synced_at": fields.Datetime.now(),
            "last_attempt_at": fields.Datetime.now(),
            "message": False,
        })

    def action_mark_failed(self):
        self.write({"status": "failed", "last_attempt_at": fields.Datetime.now()})

    def action_open_business_record(self):
        self.ensure_one()
        if not self.record_model or not self.record_id or self.record_model not in self.env:
            raise UserError(_("No business record is linked to this Sync Record."))
        record = self.env[self.record_model].sudo().browse(self.record_id).exists()
        if not record:
            raise UserError(_("The linked business record no longer exists."))
        return {
            "type": "ir.actions.act_window",
            "name": self.record_display_name or self.record_key,
            "res_model": self.record_model,
            "res_id": record.id,
            "view_mode": "form",
            "target": "current",
        }
