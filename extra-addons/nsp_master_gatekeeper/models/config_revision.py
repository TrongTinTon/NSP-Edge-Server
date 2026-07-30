# -*- coding: utf-8 -*-
from odoo import api, fields, models


def _edge_ids(records):
    ids = set()
    for rec in records:
        if rec._name == "nsp.controller" and rec.edge_server_id:
            ids.add(rec.edge_server_id.id)
        elif rec._name == "nsp.device" and rec.controller_id.edge_server_id:
            ids.add(rec.controller_id.edge_server_id.id)
        elif rec._name == "nsp.device.antenna" and rec.device_id.controller_id.edge_server_id:
            ids.add(rec.device_id.controller_id.edge_server_id.id)
        elif rec._name == "nsp.parking.area":
            ids.update(rec.edge_server_ids.ids)
        elif rec._name == "nsp.parking.lane" and rec.controller_id.edge_server_id:
            ids.add(rec.controller_id.edge_server_id.id)
        elif rec._name == "nsp.parking.antenna.transition" and rec.lane_id.controller_id.edge_server_id:
            ids.add(rec.lane_id.controller_id.edge_server_id.id)
        elif rec._name == "nsp.branch":
            ids.update(rec.parking_area_ids.mapped("edge_server_ids").ids)
    return ids


class NspEdgeServerRevision(models.Model):
    _inherit = "nsp.edge.server"
    config_revision = fields.Integer(default=1, readonly=True, copy=False, index=True)

    def bump_config_revision(self):
        for rec in self.sudo().exists():
            self.env.cr.execute(
                "UPDATE nsp_edge_server SET config_revision = config_revision + 1 WHERE id = %s",
                (rec.id,),
            )
        self.invalidate_recordset(["config_revision"])
        return True


def _bump_edge_revisions(env, ids):
    """Bump configuration revision for the affected Cloud-managed Edge nodes."""
    edge_ids = {int(edge_id) for edge_id in (ids or set()) if edge_id}
    if edge_ids:
        env["nsp.edge.server"].sudo().browse(sorted(edge_ids)).bump_config_revision()


class NspControllerRevision(models.Model):
    _inherit = "nsp.controller"
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, _edge_ids(records)); return records
    def write(self, vals):
        runtime_only = set(vals) <= {"timestamp", "status"}
        before = _edge_ids(self); result = super().write(vals)
        if not runtime_only: _bump_edge_revisions(self.env, before | _edge_ids(self))
        return result
    def unlink(self):
        ids = _edge_ids(self); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspDeviceRevision(models.Model):
    _inherit = "nsp.device"
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, _edge_ids(records)); return records
    def write(self, vals):
        runtime_only = set(vals) <= {
            "status", "last_seen", "firmware_version",
            "runtime_power_dbm", "runtime_read_interval_ms",
        }
        before = _edge_ids(self); result = super().write(vals)
        if not runtime_only: _bump_edge_revisions(self.env, before | _edge_ids(self))
        return result
    def unlink(self):
        ids = _edge_ids(self); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspAntennaRevision(models.Model):
    _inherit = "nsp.device.antenna"
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, _edge_ids(records)); return records
    def write(self, vals):
        before = _edge_ids(self); result = super().write(vals); _bump_edge_revisions(self.env, before | _edge_ids(self)); return result
    def unlink(self):
        ids = _edge_ids(self); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspParkingAreaRevision(models.Model):
    _inherit = "nsp.parking.area"
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, _edge_ids(records)); return records
    def write(self, vals):
        before = _edge_ids(self); result = super().write(vals); _bump_edge_revisions(self.env, before | _edge_ids(self)); return result
    def unlink(self):
        ids = _edge_ids(self); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspParkingLaneRevision(models.Model):
    _inherit = "nsp.parking.lane"
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, _edge_ids(records)); return records
    def write(self, vals):
        before = _edge_ids(self); result = super().write(vals); _bump_edge_revisions(self.env, before | _edge_ids(self)); return result
    def unlink(self):
        ids = _edge_ids(self); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspParkingTransitionRevision(models.Model):
    _inherit = "nsp.parking.antenna.transition"
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, _edge_ids(records)); return records
    def write(self, vals):
        before = _edge_ids(self); result = super().write(vals); _bump_edge_revisions(self.env, before | _edge_ids(self)); return result
    def unlink(self):
        ids = _edge_ids(self); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspBranchRevision(models.Model):
    _inherit = "nsp.branch"
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, _edge_ids(records)); return records
    def write(self, vals):
        before = _edge_ids(self); result = super().write(vals); _bump_edge_revisions(self.env, before | _edge_ids(self)); return result
    def unlink(self):
        ids = _edge_ids(self); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspWhitelistRevision(models.Model):
    _inherit = "nsp.device.whitelist"
    def _all_edges(self): return set(self.env["nsp.edge.server"].sudo().search([("active", "=", True)]).ids)
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, self._all_edges()); return records
    def write(self, vals):
        result = super().write(vals); _bump_edge_revisions(self.env, self._all_edges()); return result
    def unlink(self):
        ids = self._all_edges(); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result

class NspDeviceTypeRevision(models.Model):
    _inherit = "nsp.device.type"
    def _all_edges(self): return set(self.env["nsp.edge.server"].sudo().search([("active", "=", True)]).ids)
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list); _bump_edge_revisions(self.env, self._all_edges()); return records
    def write(self, vals):
        result = super().write(vals); _bump_edge_revisions(self.env, self._all_edges()); return result
    def unlink(self):
        ids = self._all_edges(); result = super().unlink(); _bump_edge_revisions(self.env, ids); return result
