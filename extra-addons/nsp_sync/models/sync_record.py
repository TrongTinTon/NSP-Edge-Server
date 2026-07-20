# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models


class NspSyncRecord(models.Model):
    """Execution ledger for outbound Cloud synchronization and retry visibility."""

    _name = "nsp.sync.record"
    _description = "NSP Sync Record"
    _order = "last_attempt_at desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    sync_job_id = fields.Many2one("nsp.sync.job", ondelete="cascade", index=True)
    sync_action_id = fields.Many2one(
        "ir.actions.core_api", related="sync_job_id.sync_action_id", store=True, readonly=True, index=True
    )
    source_code = fields.Char(string="Source", index=True)
    route_suffix = fields.Char(index=True)
    sync_action_code = fields.Char(string="Action Code", required=True, index=True)
    sync_action_name = fields.Char(string="Action", index=True)
    operation = fields.Selection([("pull", "Pull"), ("push", "Push")], required=True, default="pull", index=True)

    record_model = fields.Char(index=True)
    record_id = fields.Integer(index=True)
    record_display_name = fields.Char()
    record_key = fields.Char(required=True, index=True)

    status = fields.Selection([
        ("pending", "Pending"),
        ("synced", "Synced"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),
    ], required=True, default="pending", index=True)
    attempts = fields.Integer(default=0, readonly=True)
    message = fields.Text()
    payload_json = fields.Text()
    response_json = fields.Text()
    last_attempt_at = fields.Datetime(index=True)
    last_synced_at = fields.Datetime(index=True)

    _sql_constraints = [
        (
            "nsp_sync_record_unique",
            "unique(source_code, sync_action_code, record_key, operation)",
            "A Sync Record already exists for this source, action, key, and operation.",
        ),
    ]

    @api.depends("source_code", "sync_action_name", "record_key", "status")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = " / ".join(filter(None, [
                rec.source_code or "NSP",
                rec.sync_action_name or rec.sync_action_code,
                rec.record_key,
                rec.status,
            ]))

    @api.model
    def _json_text(self, value):
        if value in (None, False, ""):
            return False
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @api.model
    def _source_code(self, sync_job):
        return str(sync_job.edge_server_code or "NSP").strip() or "NSP"

    @api.model
    def _record_meta(self, record=False, record_key=False):
        if record:
            return {
                "record_model": record._name,
                "record_id": record.id,
                "record_display_name": record.display_name,
                "record_key": str(record_key or record.display_name or record.id),
            }
        return {
            "record_model": False,
            "record_id": 0,
            "record_display_name": False,
            "record_key": str(record_key or "-").strip() or "-",
        }

    @api.model
    def upsert_record(
        self, sync_job, action_code=False, action_name=False, route_suffix=False,
        record=False, record_key=False, status="pending", message=False,
        payload=False, response=False, operation="pull", last_synced_at=False,
    ):
        action_code = str(
            action_code
            or sync_job.sync_action_code
            or "unknown"
        ).strip()
        source_code = self._source_code(sync_job)
        meta = self._record_meta(record=record, record_key=record_key)
        domain = [
            ("source_code", "=", source_code),
            ("sync_action_code", "=", action_code),
            ("record_key", "=", meta["record_key"]),
            ("operation", "=", operation),
        ]
        current = self.sudo().search(domain, limit=1)
        now = fields.Datetime.now()
        vals = {
            "sync_job_id": sync_job.id,
            "source_code": source_code,
            "route_suffix": route_suffix or sync_job.route_suffix,
            "sync_action_code": action_code,
            "sync_action_name": action_name or sync_job.sync_action_name or action_code,
            "operation": operation,
            "status": status,
            "message": str(message or "") or False,
            "payload_json": self._json_text(payload),
            "response_json": self._json_text(response),
            "last_attempt_at": now,
            **meta,
        }
        if status == "synced":
            vals["last_synced_at"] = last_synced_at or now
        elif status == "pending":
            vals["last_synced_at"] = False
        if current:
            vals["attempts"] = int(current.attempts or 0) + 1
            current.write(vals)
            return current
        vals["attempts"] = 1
        return self.sudo().create(vals)

    @api.model
    def mark_pending(self, **kwargs):
        kwargs["status"] = "pending"
        return self.upsert_record(**kwargs)

    @api.model
    def mark_result(self, status="synced", **kwargs):
        kwargs["status"] = status if status in ("synced", "failed", "skipped") else "failed"
        return self.upsert_record(**kwargs)
