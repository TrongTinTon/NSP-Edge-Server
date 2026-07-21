# -*- coding: utf-8 -*-
import json

from odoo import api, fields, models


class NspSyncRecord(models.Model):
    """Per-record synchronization ledger and outbound retry history."""

    _name = "nsp.sync.record"
    _description = "NSP Sync Record"
    _order = "last_attempt_at desc, last_synced_at desc, id desc"
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
    operation = fields.Selection(
        [("pull", "Pull Response"), ("push", "Push Request")], required=True, default="pull", index=True
    )

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
    attempts = fields.Integer(
        string="Attempts",
        default=0,
        readonly=True,
        help="Number of consecutive outbound attempts for the current delivery cycle. It increases once whenever the same Sync Record is retried while Pending or Failed, and resets to 1 only after the previous cycle was Synced or Skipped.",
    )
    message = fields.Text()
    payload_json = fields.Text(
        string="Request Payload",
        readonly=True,
        help="Payload sent to the remote API. For Pull records, this is the shared request that produced the response item.",
    )
    response_json = fields.Text(
        string="Response Payload",
        readonly=True,
        help="Payload returned by the remote API. For Pull records, this is the individual response item applied on Edge.",
    )
    last_attempt_at = fields.Datetime(
        string="Last Send Attempt",
        index=True,
        readonly=True,
        help="Time when the most recent outbound Push request started.",
    )
    last_synced_at = fields.Datetime(
        string="Successfully Synced At",
        index=True,
        readonly=True,
        help="Time when the current record was most recently accepted or applied successfully.",
    )

    _sql_constraints = [
        (
            "nsp_sync_record_unique",
            "unique(source_code, sync_action_code, record_key, operation)",
            "A Sync Record already exists for this source, action, key, and operation.",
        ),
        (
            "nsp_sync_record_attempts_nonnegative",
            "CHECK(attempts >= 0)",
            "Send Attempts cannot be negative.",
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
    def _record_identity(
        self, sync_job, action_code=False, action_name=False, route_suffix=False,
        record=False, record_key=False, operation="pull",
    ):
        normalized_action_code = str(
            action_code or sync_job.sync_action_code or "unknown"
        ).strip()
        source_code = self._source_code(sync_job)
        meta = self._record_meta(record=record, record_key=record_key)
        domain = [
            ("source_code", "=", source_code),
            ("sync_action_code", "=", normalized_action_code),
            ("record_key", "=", meta["record_key"]),
            ("operation", "=", operation),
        ]
        vals = {
            "sync_job_id": sync_job.id,
            "source_code": source_code,
            "route_suffix": route_suffix or sync_job.route_suffix,
            "sync_action_code": normalized_action_code,
            "sync_action_name": action_name or sync_job.sync_action_name or normalized_action_code,
            "operation": operation,
            **meta,
        }
        return domain, vals

    @api.model
    def mark_pending(
        self, sync_job, action_code=False, action_name=False, route_suffix=False,
        record=False, record_key=False, message=False, payload=False,
        operation="push",
    ):
        """Start one actual outbound request and increment the attempt counter once."""
        domain, vals = self._record_identity(
            sync_job=sync_job,
            action_code=action_code,
            action_name=action_name,
            route_suffix=route_suffix,
            record=record,
            record_key=record_key,
            operation=operation,
        )
        current = self.sudo().search(domain, limit=1)
        request_payload = self._json_text(payload)
        # A retry cycle is identified by the stable Sync Record identity
        # (source + action + record key + operation), not by byte-for-byte
        # payload equality. Runtime timestamps such as last_seen_at or
        # occurred_at may legitimately change between attempts while the
        # previous delivery is still Pending/Failed.
        is_retry = bool(current and current.status in ("pending", "failed"))
        vals.update({
            "status": "pending",
            "message": str(message or "") or False,
            "payload_json": request_payload,
            "response_json": False,
            "last_attempt_at": fields.Datetime.now(),
            "attempts": int(current.attempts or 0) + 1 if is_retry else 1,
        })
        if current:
            current.write(vals)
            return current
        return self.sudo().create(vals)

    @api.model
    def mark_result(
        self, sync_job, action_code=False, action_name=False, route_suffix=False,
        record=False, record_key=False, status="synced", message=False,
        payload=False, response=False, operation="pull", last_synced_at=False,
    ):
        """Finish an attempt or record a Pull result without incrementing Attempts."""
        normalized_status = status if status in ("synced", "failed", "skipped") else "failed"
        domain, vals = self._record_identity(
            sync_job=sync_job,
            action_code=action_code,
            action_name=action_name,
            route_suffix=route_suffix,
            record=record,
            record_key=record_key,
            operation=operation,
        )
        current = self.sudo().search(domain, limit=1)
        now = fields.Datetime.now()
        vals.update({
            "status": normalized_status,
            "message": str(message or "") or False,
            "payload_json": self._json_text(payload),
            "response_json": self._json_text(response),
        })
        if normalized_status == "synced":
            vals["last_synced_at"] = last_synced_at or now

        # Defensive fallback: a Push result should normally follow mark_pending().
        # If it does not, count exactly one observed request rather than creating
        # a misleading zero-attempt Push record.
        if not current and operation == "push":
            vals.update({
                "attempts": 1,
                "last_attempt_at": now,
            })
        elif not current:
            vals["attempts"] = 0

        if current:
            current.write(vals)
            return current
        return self.sudo().create(vals)
