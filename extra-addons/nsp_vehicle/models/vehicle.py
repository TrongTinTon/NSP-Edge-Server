# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.addons.nsp_core.utils import (
    new_management_code,
    strip_empty_x2many_create_commands,
)


class Vehicle(models.Model):
    """Internal vehicle master data and RFID assignments."""

    _name = "nsp.vehicle"
    _description = "Vehicle Management"
    _inherit = ["mail.thread", "mail.activity.mixin", "image.mixin"]
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
    brand_id = fields.Many2one("nsp.reference.brand", string="Brand", ondelete="set null", tracking=True, index=True)
    model_id = fields.Many2one("nsp.reference.model", string="Model", ondelete="set null", tracking=True, index=True)
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

    def _sanitize_vehicle_card_commands(self, commands):
        commands = strip_empty_x2many_create_commands(
            commands,
            required_field="card_id",
            ignored_fields={
                "vehicle_id",
                "state",
                "assigned_at",
                "revoked_at",
            },
        )
        if not commands:
            return commands

        assignments = self.mapped("vehicle_card_ids") if self else self.env["nsp.vehicle.card"]
        removed_ids = {
            int(command[1])
            for command in commands
            if isinstance(command, (list, tuple))
            and len(command) > 1
            and command[0] in (2, 3)
            and command[1]
        }
        existing_cards = assignments.filtered(lambda rec: rec.id not in removed_ids).mapped("card_id")
        seen_card_ids = set(existing_cards.ids)
        seen_tids = {
            self.env["nsp.rfid.card"]._normalize_tid(tid)
            for tid in existing_cards.mapped("tid")
            if tid
        }

        cleaned = []
        Card = self.env["nsp.rfid.card"]
        for command in commands:
            if not isinstance(command, (list, tuple)) or len(command) < 3 or command[0] != 0:
                cleaned.append(command)
                continue

            values = command[2] if isinstance(command[2], dict) else {}
            card_id = values.get("card_id")
            if isinstance(card_id, (list, tuple)):
                card_id = card_id[0] if card_id else False
            card_id = int(card_id) if card_id else False
            tid = Card._normalize_tid(values.get("scan_tid"))
            if not card_id and tid:
                card = Card.search([("tid", "=", tid)], limit=1)
                card_id = card.id or False

            if (card_id and card_id in seen_card_ids) or (tid and tid in seen_tids):
                continue

            cleaned.append(command)
            if card_id:
                seen_card_ids.add(card_id)
                card = Card.browse(card_id)
                if card.exists() and card.tid:
                    seen_tids.add(Card._normalize_tid(card.tid))
            if tid:
                seen_tids.add(tid)

        return cleaned

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["vehicle_code"] = str(
                vals.get("vehicle_code") or new_management_code("VEH")
            ).strip().upper()
            if "vehicle_card_ids" in vals:
                vals["vehicle_card_ids"] = self._sanitize_vehicle_card_commands(
                    vals.get("vehicle_card_ids")
                )
            if vals.get("license_plate"):
                vals["license_plate"] = self._normalize_license_plate(vals["license_plate"])
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "vehicle_card_ids" in values:
            values["vehicle_card_ids"] = self._sanitize_vehicle_card_commands(
                values.get("vehicle_card_ids")
            )
        if values.get("vehicle_code"):
            values["vehicle_code"] = str(values["vehicle_code"]).strip().upper()
        if values.get("license_plate"):
            values["license_plate"] = self._normalize_license_plate(values["license_plate"])
        return super().write(values)

    @api.onchange("brand_id")
    def _onchange_brand_id(self):
        for record in self:
            if record.model_id and record.model_id.brand_id != record.brand_id:
                record.model_id = False

    @api.constrains("brand_id", "model_id")
    def _check_model_brand(self):
        for record in self:
            if record.model_id and record.model_id.brand_id != record.brand_id:
                from odoo.exceptions import ValidationError
                raise ValidationError("Vehicle Model must belong to the selected Brand.")

    def action_archive(self):
        self.write({"active": False})
        return True

    def action_unarchive(self):
        self.write({"active": True})
        return True
