# -*- coding: utf-8 -*-
from odoo import _, models
from odoo.exceptions import UserError


class NspParkingArea(models.Model):
    _inherit = "nsp.parking.area"

    def action_open_create_parking_lanes(self):
        """Open the dedicated batch Lane creation wizard for this Draft layout."""
        self.ensure_one()
        self.check_access("write")
        if self.state != "draft":
            raise UserError(_("Parking Lanes can only be created while the Parking Layout is Draft."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Create Parking Lanes"),
            "res_model": "nsp.parking.lane.create.wizard",
            "view_mode": "form",
            "views": [(
                self.env.ref(
                    "nsp_master_gatekeeper.view_nsp_parking_lane_create_wizard_form"
                ).id,
                "form",
            )],
            "target": "new",
            "context": {
                **dict(self.env.context),
                "default_parking_area_id": self.id,
            },
        }
