# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.addons.nsp_core.utils import new_management_code

NODE_STATUS=[("online","Online"),("offline","Offline"),("block","Blocked"),("revoked","Revoked"),("error","Error")]

class NspEdgeServer(models.Model):
    """Edge-local runtime projection of a published Server identity."""

    _name = "nsp.edge.server"
    _description = "NSP Edge Server Runtime"
    _rec_name = "name"
    _order = "name, edge_server_code, id"

    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True,
        copy=False, ondelete="set null", index=True,
    )
    edge_server_code = fields.Char(
        string="Edge Server Code", required=True, readonly=True,
        copy=False, index=True, default=lambda self: new_management_code("EDGE"),
    )
    name = fields.Char(string="Edge Server Name", required=True, default="NSP Edge Server")
    status = fields.Selection(NODE_STATUS, default="offline", required=True, index=True)
    active = fields.Boolean(default=True, index=True)
    cloud_removed = fields.Boolean(default=False, readonly=True, index=True, copy=False)
    controller_ids = fields.One2many("nsp.controller", "edge_server_id", string="Controllers")

    _sql_constraints = [
        ("edge_server_code_unique", "unique(edge_server_code)", "Edge Server Code must be unique."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["edge_server_code"] = str(
                values.get("edge_server_code") or new_management_code("EDGE")
            ).strip().upper()
            values["name"] = str(values.get("name") or values["edge_server_code"]).strip()
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if values.get("edge_server_code"):
            values["edge_server_code"] = str(values["edge_server_code"]).strip().upper()
        return super().write(values)


class NspController(models.Model):
    _name="nsp.controller"
    _description="NSP Controller"
    _inherit=["mail.thread","mail.activity.mixin"]
    _rec_name="controller_name"
    _order="controller_name, controller_id, id"
    whitelist_id=fields.Many2one("nsp.device.whitelist",string="Device Whitelist",readonly=True,copy=False,ondelete="set null",index=True)
    controller_id=fields.Char(string="Controller Code",required=True,readonly=True,copy=False,index=True,default=lambda self:new_management_code("CTRL"))
    controller_name=fields.Char(string="Controller Name",required=True,default="NSP Gatekeeper Controller",tracking=True)
    edge_server_id=fields.Many2one("nsp.edge.server",string="Edge Server",required=False,ondelete="restrict",index=True)
    timestamp=fields.Datetime(string="Last Heartbeat",readonly=True,copy=False,index=True)
    active=fields.Boolean(default=True,index=True)
    cloud_removed=fields.Boolean(default=False,readonly=True,index=True,copy=False)
    status=fields.Selection(NODE_STATUS,default="offline",required=True,index=True,tracking=True)
    device_ids=fields.One2many("nsp.device","controller_id",string="Readers")
    reader_count=fields.Integer(compute="_compute_reader_counts")
    _sql_constraints=[("controller_id_unique","unique(controller_id)","Controller Code must be unique.")]
    @api.depends("device_ids")
    def _compute_reader_counts(self):
        for rec in self:
            rec.reader_count = len(rec.device_ids)
    def action_open_readers(self):
        self.ensure_one(); action=self.env.ref("nsp_business_gatekeeper.nsp_device_action").sudo().read()[0]; action.update({"domain":[("controller_id","=",self.id)],"context":{"default_controller_id":self.id}}); return action
    @api.model_create_multi
    def create(self,vals_list):
        prepared=[]
        for source in vals_list:
            vals=dict(source); vals["controller_id"]=str(vals.get("controller_id") or new_management_code("CTRL")).strip().upper(); vals["controller_name"]=str(vals.get("controller_name") or vals["controller_id"]).strip(); prepared.append(vals)
        return super().create(prepared)
    def write(self,vals):
        values=dict(vals)
        if values.get("controller_id"): values["controller_id"]=str(values["controller_id"]).strip().upper()
        return super().write(values)
    @api.model
    def cron_mark_offline_controllers(self):
        import datetime
        timeout=int(self.env["ir.config_parameter"].sudo().get_param("nsp_business_gatekeeper.controller_heartbeat_timeout_sec","120") or 120)
        cutoff=fields.Datetime.now()-datetime.timedelta(seconds=max(timeout,30))
        stale=self.sudo().search([("active","=",True),("status","=","online"),("timestamp","<",cutoff)])
        if stale: stale.write({"status":"offline"})
        return True
