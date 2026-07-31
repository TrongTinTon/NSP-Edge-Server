# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class DeviceAntenna(models.Model):
    """A numbered physical RFID antenna port owned by one Reader."""

    _name = "nsp.device.antenna"
    _description = "NSP Reader Antenna"
    _rec_name = "display_name"
    _order = "device_id, antenna_no, id"

    display_name = fields.Char(string="Antenna", compute="_compute_display_name", store=True)
    whitelist_id = fields.Many2one(
        "nsp.device.whitelist", string="Device Whitelist", readonly=True, copy=False,
        ondelete="set null", index=True,
    )
    technical_code = fields.Char(string="Technical Code", readonly=True, copy=False, index=True)
    serial_number = fields.Char(string="Serial Number", copy=False, index=True)
    whitelist_management_code = fields.Char(
        string="Whitelist Antenna",
        related="whitelist_id.technical_code",
        readonly=True,
    )
    whitelist_photo = fields.Image(
        string="Photo",
        related="whitelist_id.image_128",
        readonly=True,
    )
    antenna_no = fields.Integer(
        string="Reader Antenna No",
        required=False,
        index=True,
        help="Physical antenna port number used by the RFID Reader SDK, for example 1, 2, 3 or 4.",
    )
    device_id = fields.Many2one(
        "nsp.device",
        string="Reader",
        ondelete="cascade",
        required=False,
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
    active = fields.Boolean(default=True, index=True)

    _sql_constraints = [
        ("antenna_technical_code_unique", "unique(technical_code)", "Antenna Technical Code must be unique."),
        (
            "device_antenna_unique",
            "unique(device_id, antenna_no)",
            "Antenna number must be unique per Reader.",
        ),
    ]

    @api.depends("device_id.name", "device_id.serial_number", "antenna_no", "whitelist_id.display_name", "technical_code")
    def _compute_display_name(self):
        for antenna in self:
            if not antenna.device_id:
                antenna.display_name = (
                    antenna.whitelist_id.display_name
                    or antenna.technical_code
                    or _("Unassigned Antenna")
                )
                continue
            owner = antenna.device_id.name or antenna.device_id.serial_number
            antenna.display_name = "%s / Port %s" % (
                owner, antenna.antenna_no or "-",
            )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if "antenna_no" in fields_list and not values.get("antenna_no"):
            device_id = self.env.context.get("default_device_id")
            device = self.env["nsp.device"].browse(int(device_id)).exists() if device_id else self.env["nsp.device"]
            if device:
                existing_numbers = device.antennas_ids.mapped("antenna_no")
                values["antenna_no"] = max(existing_numbers or [0]) + 1
        return values

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
        if "antenna_no" in values and int(values.get("antenna_no") or 0) < 0:
            raise ValidationError(_("Antenna number cannot be negative."))
