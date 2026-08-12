# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.addons.nsp_core.utils import new_management_code

_logger = logging.getLogger(__name__)

NODE_STATUS = [
    ("online", "Online"),
    ("offline", "Offline"),
    ("block", "Blocked"),
    ("revoked", "Revoked"),
    ("error", "Error"),
]


class NspEdgeServer(models.Model):
    """Independent Server identity.

    Deployment topology is intentionally not stored on the Server master. Server,
    Controller and Reader are associated only by Lane Calibration or Lane Configuration
    configuration records.
    """

    _name = "nsp.edge.server"
    _description = "NSP Edge Server"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"
    _order = "name, edge_server_code, id"

    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )
    edge_server_code = fields.Char(
        string="Edge Server Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("EDGE"),
        help="Stable Cloud-managed Server identity.",
    )
    name = fields.Char(
        string="Edge Server Name", required=True, default="NSP Edge Server", tracking=True,
    )
    timestamp = fields.Datetime(
        string="Last Heartbeat", readonly=True, copy=False, index=True,
    )
    status = fields.Selection(
        NODE_STATUS, default="offline", required=True, index=True, tracking=True,
    )
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        ("edge_server_code_unique", "unique(edge_server_code)", "Edge Server Code must be unique."),
    ]

    @api.model
    def name_search(self, name="", domain=None, operator="ilike", limit=100):
        search_domain = list(domain or [])
        if name:
            search_domain = [
                "|",
                ("name", operator, name),
                ("edge_server_code", operator, name),
            ] + search_domain
        records = self.search(search_domain, limit=limit)
        return [(record.id, record.display_name) for record in records]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["edge_server_code"] = str(
                values.get("edge_server_code") or new_management_code("EDGE")
            ).strip().upper()
            values["name"] = str(
                values.get("name") or values["edge_server_code"] or "NSP Edge Server"
            ).strip()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if values.get("edge_server_code"):
            values["edge_server_code"] = str(values["edge_server_code"]).strip().upper()
        return super().write(values)


class NspController(models.Model):
    """Independent Controller identity with no Server or Reader ownership fields."""

    _name = "nsp.controller"
    _description = "NSP Controller"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "controller_name"
    _order = "controller_name, controller_id, id"

    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )
    controller_id = fields.Char(
        string="Controller Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("CTRL"),
        help="Stable Cloud-managed Controller identity.",
    )
    controller_name = fields.Char(
        string="Controller Name", required=True, default="NSP Gatekeeper Controller", tracking=True,
    )
    timestamp = fields.Datetime(
        string="Last Heartbeat", readonly=True, copy=False, index=True,
    )
    active = fields.Boolean(default=True, index=True)
    status = fields.Selection(
        NODE_STATUS, default="offline", required=True, index=True, tracking=True,
    )

    _sql_constraints = [
        ("controller_id_unique", "unique(controller_id)", "Controller Code must be unique."),
    ]

    @api.model
    def name_search(self, name="", domain=None, operator="ilike", limit=100):
        search_domain = list(domain or [])
        if name:
            search_domain = [
                "|",
                ("controller_name", operator, name),
                ("controller_id", operator, name),
            ] + search_domain
        records = self.search(search_domain, limit=limit)
        return [(record.id, record.display_name) for record in records]

    @api.model
    def name_create(self, name):
        controller_name = str(name or "").strip()
        if not controller_name:
            from odoo.exceptions import UserError
            raise UserError("Controller Name is required.")
        record = self.create({"controller_name": controller_name})
        return record.id, record.display_name

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["controller_id"] = str(
                values.get("controller_id") or new_management_code("CTRL")
            ).strip().upper()
            values["controller_name"] = str(
                values.get("controller_name") or values["controller_id"] or "NSP Controller"
            ).strip()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if values.get("controller_id"):
            values["controller_id"] = str(values["controller_id"]).strip().upper()
        return super().write(values)

    def unlink(self):
        if self.env.context.get("nsp_force_delete_controller"):
            return super().unlink()
        self.write({
            "active": False,
            "status": "revoked",
            "timestamp": fields.Datetime.now(),
        })
        return True

    def action_archive(self):
        self.check_access("write")
        self.write({"active": False, "status": "revoked"})
        return True

    def action_unarchive(self):
        self.check_access("write")
        self.write({"active": True, "status": "offline"})
        return True

    @api.model
    def cron_mark_offline_controllers(self):
        parameter = self.env["ir.config_parameter"].sudo().get_param(
            "nsp_master_gatekeeper.controller_heartbeat_timeout_sec",
            "120",
        )
        try:
            timeout_sec = int(parameter or "120")
        except (TypeError, ValueError):
            _logger.warning(
                "Invalid controller heartbeat timeout %r; using 120 seconds.",
                parameter,
            )
            timeout_sec = 120

        cutoff = fields.Datetime.now() - timedelta(seconds=max(30, timeout_sec))
        system_context = {
            "active_test": False,
            "tracking_disable": True,
            "mail_notrack": True,
        }
        stale_controllers = self.sudo().with_context(**system_context).search([
            ("status", "not in", ("offline", "revoked")),
            "|",
            ("timestamp", "=", False),
            ("timestamp", "<", cutoff),
        ])
        stale_controllers.write({"status": "offline"})

        stale_edges = self.env["nsp.edge.server"].sudo().with_context(
            **system_context
        ).search([
            ("status", "not in", ("offline", "revoked")),
            "|",
            ("timestamp", "=", False),
            ("timestamp", "<", cutoff),
        ])
        stale_edges.write({"status": "offline"})
        return True
