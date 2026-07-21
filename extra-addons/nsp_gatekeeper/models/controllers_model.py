# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.addons.nsp_core.utils import new_management_code

NODE_STATUS = [
    ("online", "Online"),
    ("offline", "Offline"),
    ("block", "Blocked"),
    ("revoked", "Revoked"),
    ("error", "Error"),
]

class NspEdgeServer(models.Model):
    _name = "nsp.edge.server"
    _description = "NSP Edge Server"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "edge_server_code"
    _order = "name, edge_server_code, id"

    edge_server_code = fields.Char(
        string="Edge Server Code", required=True, copy=False, index=True, tracking=True,
        default=lambda self: new_management_code("EDGE"),
        help="Stable code assigned to this Edge Server by the Cloud Server.",
    )
    name = fields.Char(string="Edge Server Name", required=True, default="NSP Edge Server", tracking=True)
    timestamp = fields.Datetime(string="Last Heartbeat", readonly=True, copy=False, index=True)
    status = fields.Selection(NODE_STATUS, default="offline", required=True, index=True, tracking=True)
    active = fields.Boolean(default=True, index=True)
    controller_ids = fields.One2many("nsp.controller", "edge_server_id", string="Controllers")
    controller_count = fields.Integer(string="Controllers", compute="_compute_controller_count")
    reader_count = fields.Integer(string="Readers", compute="_compute_controller_count")

    _sql_constraints = [
        ("edge_server_code_unique", "unique(edge_server_code)", "Edge Server Code must be unique."),
    ]

    @api.depends("controller_ids", "controller_ids.device_ids")
    def _compute_controller_count(self):
        for record in self:
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(record.controller_ids.mapped("device_ids"))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["edge_server_code"] = str(
                vals.get("edge_server_code") or new_management_code("EDGE")
            ).strip().upper()
            vals["name"] = str(vals.get("name") or vals["edge_server_code"] or "NSP Edge Server").strip()
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if values.get("edge_server_code"):
            values["edge_server_code"] = str(values["edge_server_code"]).strip().upper()
        return super().write(values)

    def action_open_controllers(self):
        self.ensure_one()
        action = self.env.ref("nsp_gatekeeper.action_nsp_controllers").sudo().read()[0]
        action.update({
            "name": _("Controllers"),
            "domain": [("edge_server_id", "=", self.id)],
            "context": {"default_edge_server_id": self.id},
        })
        return action

    def action_open_readers(self):
        self.ensure_one()
        action = self.env.ref("nsp_gatekeeper.nsp_device_action").sudo().read()[0]
        action.update({
            "name": _("Readers"),
            "domain": [("controller_id.edge_server_id", "=", self.id)],
            "context": {},
        })
        return action

    def unlink(self):
        if self.controller_ids:
            raise UserError(_("Move or archive the Controllers assigned to this Edge Server first."))
        return super().unlink()

class NspController(models.Model):
    _name = "nsp.controller"
    _description = "NSP Controller"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "controller_id"
    _order = "edge_server_id, controller_name, controller_id, id"

    controller_id = fields.Char(
        string="Controller Code", required=True, copy=False, index=True, tracking=True,
        default=lambda self: new_management_code("CTRL"),
        help="Stable Controller Code provisioned by the server.",
    )
    controller_name = fields.Char(string="Controller Name", required=True, default="NSP Gatekeeper Controller", tracking=True)
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Edge Server", required=True, ondelete="restrict", index=True, tracking=True,
        help="Edge Server responsible for synchronization with this Controller. This is a direct relation, not a parent/child Controller hierarchy.",
    )
    timestamp = fields.Datetime(string="Last Heartbeat", readonly=True, copy=False, index=True)
    active = fields.Boolean(default=True, index=True)
    status = fields.Selection(NODE_STATUS, default="offline", required=True, index=True, tracking=True)
    last_device_report_at = fields.Datetime(string="Last Reader Report", readonly=True, copy=False)
    device_ids = fields.One2many("nsp.device", "controller_id", string="Readers")
    reader_count = fields.Integer(string="Readers", compute="_compute_reader_counts")
    antenna_count = fields.Integer(string="Antennas", compute="_compute_reader_counts")
    _sql_constraints = [
        ("controller_id_unique", "unique(controller_id)", "Controller Code must be unique."),
    ]

    @api.depends("device_ids", "device_ids.antennas_ids")
    def _compute_reader_counts(self):
        for record in self:
            record.reader_count = len(record.device_ids)
            record.antenna_count = len(record.device_ids.mapped("antennas_ids"))

    def action_open_readers(self):
        self.ensure_one()
        action = self.env.ref("nsp_gatekeeper.nsp_device_action").sudo().read()[0]
        action.update({
            "name": _("Readers"),
            "domain": [("controller_id", "=", self.id)],
            "context": {"default_controller_id": self.id},
        })
        return action

    def action_open_antennas(self):
        self.ensure_one()
        action = self.env.ref("nsp_gatekeeper.action_nsp_device_antenna").sudo().read()[0]
        action.update({
            "name": _("Antennas"),
            "domain": [("controller_id", "=", self.id)],
            "context": {},
        })
        return action

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["controller_id"] = str(
                vals.get("controller_id") or new_management_code("CTRL")
            ).strip().upper()
            vals["controller_name"] = str(vals.get("controller_name") or vals["controller_id"] or "NSP Controller").strip()
            prepared.append(vals)
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
        self.write({"active": False, "status": "revoked"})
        return True

    def action_unarchive(self):
        self.write({"active": True, "status": "offline"})
        return True

    @api.model
    def cron_mark_offline_controllers(self):
        try:
            timeout_sec = int(self.env["ir.config_parameter"].sudo().get_param(
                "nsp_gatekeeper.controller_heartbeat_timeout_sec", "120"
            ) or "120")
        except Exception:
            timeout_sec = 120
        timeout_sec = max(30, timeout_sec)
        self.env.cr.execute("""
            UPDATE nsp_controller
               SET status = 'offline'
             WHERE COALESCE(status, 'offline') NOT IN ('offline', 'revoked')
               AND (timestamp IS NULL OR timestamp < (NOW() AT TIME ZONE 'UTC') - (%s || ' seconds')::interval)
        """, (str(timeout_sec),))
        self.env.cr.execute("""
            UPDATE nsp_edge_server
               SET status = 'offline'
             WHERE COALESCE(status, 'offline') NOT IN ('offline', 'revoked')
               AND (timestamp IS NULL OR timestamp < (NOW() AT TIME ZONE 'UTC') - (%s || ' seconds')::interval)
        """, (str(timeout_sec),))
        self.env.cr.execute("""
            UPDATE nsp_device AS device
               SET status = 'offline'
              FROM nsp_controller AS controller
             WHERE device.controller_id = controller.id
               AND COALESCE(device.status, 'offline') != 'offline'
               AND COALESCE(controller.status, 'offline') = 'offline'
        """)
        return True
