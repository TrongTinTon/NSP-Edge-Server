# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class DeviceAntenna(models.Model):
    """A numbered RFID antenna port with an optional RSSI acceptance threshold."""

    _name = "nsp.device.antenna"
    _description = "NSP Reader Antenna"
    _rec_name = "display_name"
    _order = "device_id, antenna_no, id"

    display_name = fields.Char(string="Antenna", compute="_compute_display_name", store=True)
    antenna_no = fields.Integer(string="Antenna No", required=True, index=True)
    minimum_rssi_dbm = fields.Float(
        string="Minimum RSSI (dBm)",
        required=True,
        default=-65.0,
        help="The Controller ignores detections weaker than this threshold. A value closer to 0 accepts only stronger, usually nearer, tags.",
    )
    device_id = fields.Many2one(
        "nsp.device",
        string="Reader",
        ondelete="cascade",
        required=True,
        index=True,
    )
    device_serial = fields.Char(
        string="Reader Serial",
        related="device_id.serial_number",
        readonly=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        related="device_id.controller_id",
        readonly=True,
    )
    lane_rule_ids = fields.One2many(
        "nsp.parking.lane.antenna.mapping",
        "antenna_ref_id",
        string="Parking Lane Antenna Mapping",
        readonly=True,
    )
    lane_count = fields.Integer(string="Mapped Lanes", compute="_compute_lane_count")

    _sql_constraints = [
        (
            "device_antenna_unique",
            "unique(device_id, antenna_no)",
            "Antenna number must be unique per Reader.",
        ),
        (
            "antenna_no_positive",
            "CHECK(antenna_no > 0)",
            "Antenna number must be greater than zero.",
        ),
        (
            "antenna_rssi_range",
            "CHECK(minimum_rssi_dbm >= -120 AND minimum_rssi_dbm <= 0)",
            "Minimum RSSI must be between -120 and 0 dBm.",
        ),
    ]

    @api.depends("device_id.name", "device_id.serial_number", "antenna_no")
    def _compute_display_name(self):
        for antenna in self:
            antenna.display_name = "%s / Antenna %s" % (
                antenna.device_id.name or antenna.device_id.serial_number or _("Reader"),
                antenna.antenna_no or "",
            )

    @api.depends("lane_rule_ids")
    def _compute_lane_count(self):
        for antenna in self:
            antenna.lane_count = len(antenna.lane_rule_ids)

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            self._validate_values(vals)
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        self._validate_values(values, partial=True)
        return super().write(values)

    @api.model
    def _validate_values(self, values, partial=False):
        if "antenna_no" in values or not partial:
            if int(values.get("antenna_no") or 0) <= 0:
                raise ValidationError(_("Antenna number must be greater than zero."))
        if "minimum_rssi_dbm" in values:
            rssi = float(values.get("minimum_rssi_dbm") or 0.0)
            if rssi < -120 or rssi > 0:
                raise ValidationError(_("Minimum RSSI must be between -120 and 0 dBm."))
