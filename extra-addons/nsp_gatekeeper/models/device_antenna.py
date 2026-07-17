from odoo import api, fields, models, _
from odoo.http import request
from odoo.exceptions import ValidationError

class DeviceAntenna(models.Model):
    """Device antennas"""
    _name = "nsp.device.antenna"
    _description = "NSP Device Antenna"
    _rec_name    = "antenna_id"
    _order = "device_id, antenna_id, id"

    antenna_id      = fields.Integer(string="Antenna No", required=True)
    status          = fields.Char(string="Status", help="Operating status of the antenna", default="Inactive")
    power_dbm       = fields.Integer(string="Power dBm", help="RF output power of this antenna when reported by the reader")
    return_loss_db  = fields.Integer(string="Return Loss dB", help="Antenna connection detector return loss value when available")

    scan_time       = fields.Integer(string="Scan Time", help="Card scan time of each antenna")
    q_value         = fields.Integer(string="Q-Value")
    session         = fields.Integer(string="Session")

    is_active       = fields.Boolean(string="Active?", default=False)

    # one device may have many antennas
    device_id = fields.Many2one("nsp.device", string="Device", ondelete="cascade", required=True, index=True, help="Device that owns this antenna")
    device_serial = fields.Char(string="Device Serial", related="device_id.serial_number", store=True, readonly=True, index=True)
    controller_id = fields.Many2one("nsp.controller", string="Controller", related="device_id.controller_id", store=True, readonly=True, index=True)
    lane_rule_ids = fields.One2many("nsp.gate.lane.antenna.mapping", "antenna_ref_id", string="Lane Mapping", readonly=True)
    lane_count = fields.Integer(string="Mapped Lanes", compute="_compute_lane_count", store=False)

    _sql_constraints = [
        ("device_antenna_unique", "unique(device_id, antenna_id)", "Antenna number must be unique per Device."),
    ]

    @api.depends("lane_rule_ids", "lane_rule_ids.is_active")
    def _compute_lane_count(self):
        for antenna in self:
            antenna.lane_count = len(antenna.lane_rule_ids.filtered(lambda r: r.is_active))

    @api.constrains('scan_time')
    def _check_scan_time_boundaries(self):
        """Scan time must be in the range 0 - 255"""
        for record in self:
            if record.scan_time is not None:
                if record.scan_time < 0 or record.scan_time > 255:
                    raise ValidationError(_("Scan Time must be in the range 0 - 255!"))
    
    @api.constrains('q_value')
    def _check_q_value_boundaries(self):
        """QValue must be in the range 0 - 15"""
        for record in self:
            if record.q_value is not None:
                if record.q_value < 0 or record.q_value > 15:
                    raise ValidationError(_("QValue must be in the range 0 - 15!"))
                
    @api.constrains('session')
    def _check_session_boundaries(self):
        """Session must be in the range 0 - 3"""
        for record in self:
            if record.session is not None:
                if record.session < 0 or record.session > 3:
                    raise ValidationError(_("Session must be in the range 0 - 3!"))

    @api.model
    def create(self, vals):
        """Create device"""
        antennas = super().create(vals)
        return antennas
    
    def write(self, vals):
        """Update antenna and mark parent device config as not synced."""
        result = super().write(vals)
        config_fields = {'scan_time', 'q_value', 'session', 'power_dbm', 'return_loss_db', 'is_active', 'status'}
        if config_fields.intersection(vals.keys()):
            for device in self.mapped('device_id'):
                device.write({'config_sync_status': 'not_synced', 'config_applied_status': 'not_applied'})
        return result
