# -*- coding: utf-8 -*-
from odoo import fields, models


class NspEdgeServerRevision(models.Model):
    """Revision of the last published runtime configuration for one Edge.

    Editable Device Whitelist, Lane Calibration and Parking Layout records do
    not bump this value. A revision changes only when a Parking Layout snapshot
    is explicitly published for the Edge.
    """

    _inherit = "nsp.edge.server"

    config_revision = fields.Integer(default=1, readonly=True, copy=False, index=True)

    def bump_config_revision(self):
        for record in self.sudo().exists():
            self.env.cr.execute(
                "UPDATE nsp_edge_server "
                "SET config_revision = config_revision + 1 "
                "WHERE id = %s",
                (record.id,),
            )
        self.invalidate_recordset(["config_revision"])
        return True
