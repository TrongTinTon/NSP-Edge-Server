# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
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
        help="Stable code assigned to this Edge Server by the Cloud Server.",
    )
    name = fields.Char(string="Edge Server Name", required=True, default="NSP Edge Server", tracking=True)
    timestamp = fields.Datetime(string="Last Heartbeat", readonly=True, copy=False, index=True)
    status = fields.Selection(NODE_STATUS, default="offline", required=True, index=True, tracking=True)
    active = fields.Boolean(default=True, index=True)
    controller_ids = fields.One2many("nsp.controller", "edge_server_id", string="Controllers")
    reader_ids = fields.Many2many(
        "nsp.device",
        string="Readers",
        compute="_compute_inventory",
        readonly=True,
        help="Readers managed by the Controllers assigned to this Edge Server.",
    )
    antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        string="Antennas",
        compute="_compute_inventory",
        readonly=True,
        help="Physical antennas declared on Readers managed by this Edge Server.",
    )
    controller_count = fields.Integer(string="Controllers", compute="_compute_inventory")
    reader_count = fields.Integer(string="Readers", compute="_compute_inventory")
    antenna_count = fields.Integer(string="Antennas", compute="_compute_inventory")

    _sql_constraints = [
        ("edge_server_code_unique", "unique(edge_server_code)", "Edge Server Code must be unique."),
    ]

    @api.depends(
        "controller_ids",
        "controller_ids.device_ids",
        "controller_ids.device_ids.antennas_ids",
    )
    def _compute_inventory(self):
        for record in self:
            readers = record.controller_ids.mapped("device_ids")
            antennas = readers.mapped("antennas_ids")
            record.reader_ids = readers
            record.antenna_ids = antennas
            record.controller_count = len(record.controller_ids)
            record.reader_count = len(readers)
            record.antenna_count = len(antennas)

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

    def unlink(self):
        if self.controller_ids:
            raise UserError(_("Move or archive the Controllers assigned to this Edge Server first."))
        return super().unlink()

class NspController(models.Model):
    _name = "nsp.controller"
    _description = "NSP Controller"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "controller_name"
    _order = "edge_server_id, controller_name, controller_id, id"

    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )
    controller_id = fields.Char(
        string="Controller Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("CTRL"),
        help="Stable Controller Code provisioned by the server.",
    )
    controller_name = fields.Char(string="Controller Name", required=True, default="NSP Gatekeeper Controller", tracking=True)
    edge_server_id = fields.Many2one(
        "nsp.edge.server", string="Edge Server", required=False, ondelete="restrict", index=True, tracking=True,
        help="Edge Server responsible for synchronization with this Controller. This is a direct relation, not a parent/child Controller hierarchy.",
    )
    timestamp = fields.Datetime(string="Last Heartbeat", readonly=True, copy=False, index=True)
    active = fields.Boolean(default=True, index=True)
    status = fields.Selection(NODE_STATUS, default="offline", required=True, index=True, tracking=True)
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

    @api.onchange("device_ids")
    def _onchange_reader_serials_unique(self):
        Device = self.env["nsp.device"]
        for controller in self:
            seen = set()
            for reader in controller.device_ids:
                serial = Device._normalize_serial(reader.serial_number)
                if not serial:
                    continue
                if serial in seen:
                    Device._raise_serial_conflict(serial)
                seen.add(serial)

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
            raise UserError(_("Controller Name is required."))

        edge = self.env["nsp.edge.server"]
        edge_id = self.env.context.get("default_edge_server_id")
        if edge_id:
            edge = edge.browse(int(edge_id)).exists()
        else:
            candidates = edge.search([("active", "=", True)], limit=2)
            if len(candidates) == 1:
                edge = candidates

        if len(edge) != 1:
            raise UserError(_(
                "Select an Edge Server first, or use Create and Edit... to create the Controller with its Edge Server."
            ))

        record = self.create({
            "controller_name": controller_name,
            "edge_server_id": edge.id,
        })
        return record.id, record.display_name

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
        system_context = {"active_test": False, "tracking_disable": True, "mail_notrack": True}

        # This cron owns the global node-liveness scope.  The elevated access is
        # intentionally limited to the three batch updates below.
        stale_controllers = self.sudo().with_context(**system_context).search([
            ("status", "not in", ("offline", "revoked")),
            "|",
            ("timestamp", "=", False),
            ("timestamp", "<", cutoff),
        ])
        stale_controllers.write({"status": "offline"})

        EdgeServer = self.env["nsp.edge.server"].sudo().with_context(**system_context)
        stale_edges = EdgeServer.search([
            ("status", "not in", ("offline", "revoked")),
            "|",
            ("timestamp", "=", False),
            ("timestamp", "<", cutoff),
        ])
        stale_edges.write({"status": "offline"})

        Device = self.env["nsp.device"].sudo().with_context(**system_context)
        readers_on_offline_controllers = Device.search([
            ("status", "!=", "offline"),
            ("controller_id.status", "=", "offline"),
        ])
        readers_on_offline_controllers.write({"status": "offline"})
        return True
