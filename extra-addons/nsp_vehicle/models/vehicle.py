# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class Vehicle(models.Model):
    """Manage vehicle list and identification tags"""
    _name = "nsp.vehicle"
    _description = "Vehicle Management"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = "license_plate"

    # --- Basic Information ---
    license_plate = fields.Char(string="License Plate", required=True, tracking=True, store=True)
    owner_id = fields.Many2one('nsp.user', string="Owner / User", required=True, tracking=True, store=True)
    # Master data fields. Users can quick-create values directly from the dropdown.
    vehicle_type_id = fields.Many2one('nsp.vehicle.type', string="Vehicle Type", ondelete="set null", tracking=True)
    brand_id = fields.Many2one('nsp.vehicle.brand', string="Brand", ondelete="set null", tracking=True)
    model_id = fields.Many2one('nsp.vehicle.model', string="Model", ondelete="set null", tracking=True)
    color_id = fields.Many2one('nsp.vehicle.color', string="Color", ondelete="set null", tracking=True)

    # --- RFID Card Information ---
    vehicle_card_ids = fields.One2many(
        'nsp.vehicle.card',
        'vehicle_id',
        string="Vehicle Cards",
        help="All cards assigned to this vehicle. Only Active cards are synced to Controller.",
    )
    tid = fields.Char(
        string="Primary Active TID",
        compute='_compute_vehicle_card_tids',
        store=False,
        readonly=True,
        copy=False,
        help="First active Vehicle Card TID for display and API payload convenience. Not stored; Master Card remains the source of truth.",
    )
    vehicle_tid_tids = fields.Char(string="All Active Vehicle TIDs", compute='_compute_vehicle_card_tids', store=False, readonly=True)
    active_vehicle_card_count = fields.Integer(string="Active Vehicle Cards", compute='_compute_vehicle_card_tids', store=False)

    @api.depends('vehicle_card_ids.state', 'vehicle_card_ids.tid', 'vehicle_card_ids.card_id.tid')
    def _compute_vehicle_card_tids(self):
        for rec in self:
            active_cards = rec.vehicle_card_ids.filtered(lambda line: line.state == 'active' and line.tid)
            tids = active_cards.mapped('tid')
            rec.tid = tids[0] if tids else False
            rec.vehicle_tid_tids = ','.join(tids) if tids else False
            rec.active_vehicle_card_count = len(tids)

    state = fields.Selection([
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected')
    ], string="Status", default='pending', tracking=True)
    reject_reason = fields.Text(string="Reject Reason", tracking=True)

    # --- Constraints ---

    _sql_constraints = [
        ('license_plate_uniq', 'unique(license_plate)', 'This license plate already exists in the system!'),
    ]

    @api.model
    def _normalize_license_plate(self, value):
        if not value:
            return value
        # Keep the original plate format readable, but remove accidental leading/trailing spaces,
        # collapse internal whitespace, and uppercase to enforce practical uniqueness.
        return ' '.join(str(value).strip().upper().split())

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('license_plate'):
                vals['license_plate'] = self._normalize_license_plate(vals['license_plate'])
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('license_plate'):
            vals = dict(vals)
            vals['license_plate'] = self._normalize_license_plate(vals['license_plate'])
        return super().write(vals)

    @api.constrains('license_plate')
    def _check_license_plate_unique_normalized(self):
        for rec in self:
            if not rec.license_plate:
                continue
            normalized = rec._normalize_license_plate(rec.license_plate)
            self.env.cr.execute(
                """
                SELECT id FROM nsp_vehicle
                 WHERE id <> %s
                   AND UPPER(TRIM(license_plate)) = UPPER(TRIM(%s))
                 LIMIT 1
                """,
                (rec.id, normalized),
            )
            if self.env.cr.fetchone():
                raise ValidationError(_("This license plate already exists in the system!"))

    def action_open_grant_card_wizard(self):
        """Open Wizard to grant card by scanning/assigning TID from RFID Cards."""
        self.ensure_one()
        return {
            'name': _('Grant RFID Card for vehicle %s') % self.license_plate,
            'type': 'ir.actions.act_window',
            'res_model': 'nsp.grant.card.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_vehicle_id': self.id,
                'default_current_tid': self.tid,
            }
        }

    def action_approve(self):
        """Approve vehicle button for Admin"""
        for rec in self:
            rec.state = 'approved'

    def action_reject(self):
        # Popup reject(Wizard)
        return {
            'name': 'Enter Reject Reason',
            'type': 'ir.actions.act_window',
            'res_model': 'nsp.vehicle.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_vehicle_id': self.id}
        }
