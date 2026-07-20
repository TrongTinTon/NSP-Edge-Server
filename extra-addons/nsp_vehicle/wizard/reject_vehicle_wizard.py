from odoo import fields, models

class RejectVehicleWizard(models.TransientModel):
    _name = 'nsp.vehicle.reject.wizard'
    _description = 'Wizard for providing reason to reject a vehicle'

    vehicle_id = fields.Many2one('nsp.vehicle', string="Vehicle", required=True)
    reason = fields.Text(string="Reason for Rejection", required=True)

    def action_confirm_reject(self):
        self.vehicle_id.write({
            'state': 'rejected',
            'reject_reason': self.reason
        })