# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class NspParkingLaneCreateWizard(models.TransientModel):
    _name = "nsp.parking.lane.create.wizard"
    _description = "Create Parking Lanes"

    parking_area_id = fields.Many2one(
        "nsp.parking.area",
        string="Parking Layout",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    line_ids = fields.One2many(
        "nsp.parking.lane.create.line",
        "wizard_id",
        string="Parking Lanes",
    )
    available_edge_server_ids = fields.Many2many(
        "nsp.edge.server",
        compute="_compute_available_devices",
        string="Available Servers",
    )
    available_controller_ids = fields.Many2many(
        "nsp.controller",
        compute="_compute_available_devices",
        string="Available Controllers",
    )
    lane_count = fields.Integer(
        string="Lanes to Create",
        compute="_compute_lane_count",
    )

    @api.model
    def default_get(self, field_list):
        values = super().default_get(field_list)
        parking_area_id = values.get("parking_area_id") or self.env.context.get(
            "default_parking_area_id"
        )
        if parking_area_id and "line_ids" in field_list and not values.get("line_ids"):
            area = self.env["nsp.parking.area"].browse(int(parking_area_id)).exists()
            next_number = len(area.lane_ids) + 1 if area else 1
            values["line_ids"] = [(0, 0, {"name": _("Lane %s") % next_number})]
        return values

    @api.depends("line_ids")
    def _compute_lane_count(self):
        for wizard in self:
            wizard.lane_count = len(wizard.line_ids)

    @api.depends("parking_area_id")
    def _compute_available_devices(self):
        Edge = self.env["nsp.edge.server"]
        Controller = self.env["nsp.controller"]
        edges = Edge.search([
            ("active", "=", True),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", "SERVER"),
        ])
        controllers = Controller.search([
            ("active", "=", True),
            ("whitelist_id.active", "=", True),
            ("whitelist_id.device_type_code", "=", "CONTROLLER"),
        ])
        for wizard in self:
            wizard.available_edge_server_ids = edges
            wizard.available_controller_ids = controllers

    def _validate_lines(self):
        self.ensure_one()
        area = self.parking_area_id.exists()
        if not area:
            raise ValidationError(_("Parking Layout no longer exists."))
        area.check_access("write")
        if area.state != "draft":
            raise ValidationError(_("Parking Lanes can only be created in a Draft Parking Layout."))
        if not self.line_ids:
            raise ValidationError(_("Add at least one Parking Lane."))

        names = []
        for line in self.line_ids:
            lane_name = str(line.name or "").strip()
            if not lane_name:
                raise ValidationError(_("Lane Name is required."))
            if not line.edge_server_id:
                raise ValidationError(_("Select a Server for %(lane)s.") % {"lane": lane_name})
            if not line.controller_id:
                raise ValidationError(_("Select a Controller for %(lane)s.") % {"lane": lane_name})
            names.append(lane_name.casefold())

        if len(names) != len(set(names)):
            raise ValidationError(_("Lane Names must be unique within this creation batch."))

        existing_names = {
            str(name or "").strip().casefold()
            for name in area.lane_ids.mapped("name")
            if str(name or "").strip()
        }
        duplicates = sorted({name for name in names if name in existing_names})
        if duplicates:
            raise ValidationError(_("A Lane with the same name already exists in this Parking Layout."))
        return area

    def action_create_lanes(self):
        self.ensure_one()
        area = self._validate_lines()
        vals_list = [
            {
                "name": str(line.name or "").strip(),
                "parking_area_id": area.id,
                "edge_server_id": line.edge_server_id.id,
                "controller_id": line.controller_id.id,
                "setup_state": "draft",
                "active": True,
            }
            for line in self.line_ids
        ]
        self.env["nsp.parking.lane"].create(vals_list)
        return {
            "type": "ir.actions.act_window",
            "name": _("Parking Layout"),
            "res_model": "nsp.parking.area",
            "res_id": area.id,
            "view_mode": "form",
            "views": [(
                self.env.ref("nsp_master_gatekeeper.view_nsp_parking_area_form").id,
                "form",
            )],
            "target": "current",
        }


class NspParkingLaneCreateLine(models.TransientModel):
    _name = "nsp.parking.lane.create.line"
    _description = "Parking Lane Creation Line"
    _order = "id"

    wizard_id = fields.Many2one(
        "nsp.parking.lane.create.wizard",
        required=True,
        ondelete="cascade",
        index=True,
    )
    name = fields.Char(string="Lane Name", required=True)
    edge_server_id = fields.Many2one(
        "nsp.edge.server",
        string="Server",
        required=True,
        ondelete="restrict",
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        required=True,
        ondelete="restrict",
    )
    available_edge_server_ids = fields.Many2many(
        "nsp.edge.server",
        related="wizard_id.available_edge_server_ids",
        readonly=True,
    )
    available_controller_ids = fields.Many2many(
        "nsp.controller",
        related="wizard_id.available_controller_ids",
        readonly=True,
    )
