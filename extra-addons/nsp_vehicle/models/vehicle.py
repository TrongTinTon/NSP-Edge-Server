# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.addons.nsp_core.utils import new_management_code


class Vehicle(models.Model):
    """Internal vehicle master data and RFID assignments."""

    _name = "nsp.vehicle"
    _description = "Vehicle Management"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "license_plate"
    _order = "license_plate, id"

    vehicle_code = fields.Char(
        string="Technical Code", required=True, readonly=True, copy=False, index=True,
        default=lambda self: new_management_code("VEH"),
        help="Stable system-generated identifier used for Cloud/Edge synchronization.",
    )
    license_plate = fields.Char(string="License Plate", required=True, tracking=True, index=True)
    owner_id = fields.Many2one(
        "nsp.user", string="Owner", required=True, tracking=True,
        ondelete="restrict", index=True,
    )
    vehicle_type_id = fields.Many2one("nsp.vehicle.type", string="Vehicle Type", ondelete="set null", tracking=True)
    brand_id = fields.Many2one("nsp.vehicle.brand", string="Brand", ondelete="set null", tracking=True)
    model_id = fields.Many2one("nsp.vehicle.model", string="Model", ondelete="set null", tracking=True)
    color_id = fields.Many2one("nsp.vehicle.color", string="Color", ondelete="set null", tracking=True)
    active = fields.Boolean(default=True, tracking=True, index=True)

    vehicle_card_ids = fields.One2many(
        "nsp.vehicle.card", "vehicle_id", string="Vehicle Cards",
        help="All cards assigned to this vehicle. Only active assignments are synchronized.",
    )
    tid = fields.Char(
        string="Primary Active TID", compute="_compute_vehicle_card_tids",
        readonly=True, copy=False,
        help="First active Vehicle Card TID for display/API convenience. Master Card is the source of truth.",
    )
    vehicle_tid_tids = fields.Char(
        string="All Active Vehicle TIDs", compute="_compute_vehicle_card_tids", readonly=True,
    )
    active_vehicle_card_count = fields.Integer(
        string="Active Vehicle Cards", compute="_compute_vehicle_card_tids",
    )
    borrow_ids = fields.One2many(
        "nsp.vehicle.borrow", "vehicle_id", string="Authorized Users",
        help="Temporary vehicle-use permissions granted by the owner to accepted friends.",
    )

    _sql_constraints = [
        ("vehicle_code_uniq", "unique(vehicle_code)", "Vehicle Technical Code must be unique."),
        ("license_plate_uniq", "unique(license_plate)", "This license plate already exists in the system!"),
    ]

    @api.depends("vehicle_card_ids.state", "vehicle_card_ids.card_id.tid")
    def _compute_vehicle_card_tids(self):
        for rec in self:
            tids = rec.vehicle_card_ids.filtered(
                lambda line: line.state == "active" and line.card_id.tid
            ).mapped("card_id.tid")
            rec.tid = tids[0] if tids else False
            rec.vehicle_tid_tids = ",".join(tids) if tids else False
            rec.active_vehicle_card_count = len(tids)

    @api.model
    def _normalize_license_plate(self, value):
        if not value:
            return value
        return " ".join(str(value).strip().upper().split())

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["vehicle_code"] = str(
                vals.get("vehicle_code") or new_management_code("VEH")
            ).strip().upper()
            if vals.get("license_plate"):
                vals["license_plate"] = self._normalize_license_plate(vals["license_plate"])
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if values.get("vehicle_code"):
            values["vehicle_code"] = str(values["vehicle_code"]).strip().upper()
        if values.get("license_plate"):
            values["license_plate"] = self._normalize_license_plate(values["license_plate"])
        return super().write(values)

    def action_archive(self):
        self.write({"active": False})
        return True

    def action_unarchive(self):
        self.write({"active": True})
        return True

    def action_open_grant_card_wizard(self):
        """Open the scan/assign helper for operators who grant a card by TID."""
        self.ensure_one()
        return {
            "name": _("Grant RFID Card for vehicle %s") % self.license_plate,
            "type": "ir.actions.act_window",
            "res_model": "nsp.grant.card.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_vehicle_id": self.id,
                "default_current_tid": self.tid,
            },
        }
