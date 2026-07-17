# -*- coding: utf-8 -*-
from odoo import models, fields, api
from odoo.tools.safe_eval import safe_eval

class IrActionsServer(models.Model):
    _inherit = 'ir.actions.server'

    endpoint_manager_id = fields.Many2one(
        'action.endpoint.manager', 
        string='Endpoint Manager', 
        ondelete='cascade'
    )

class IrActionsCoreApi(models.Model):
    _name = 'ir.actions.core_api'
    _description = 'Action: Only Execute Python Code'
    _inherit = 'ir.actions.actions'

    type = fields.Char(default='ir.actions.core_api')

    endpoint_manager_id = fields.Many2one(
        'action.endpoint.manager', 
        string='Endpoint Manager', 
        ondelete='cascade'
    )

    model_id = fields.Many2one('ir.model', string='Model', required=True, ondelete='cascade')
    code = fields.Text(string='Python Code', required=True)
    endpoint_code = fields.Char(
        string='Action Code',
        index=True,
        help='Stable API action code declared by the @endpoint decorator, e.g. inventory_product_sync.',
    )
    route_suffix = fields.Char(
        string='Route Path',
        help='Route Path only, e.g. vehicles/sync. Core API derives the gateway path from Application and API Version.',
    )
    http_methods = fields.Char(
        string='Allowed Methods',
        default='POST',
        help='Comma-separated HTTP methods declared by the @endpoint decorator.',
    )

    _unique_name = models.Constraint(
        'UNIQUE(endpoint_manager_id, name)',
        'unique name with manager',
    )

    @api.model
    def run(self):
        self.ensure_one()
        ctx = self.env.context

        if self.model_id.model not in self.env:
            raise ValueError(f"Model {self.model_id.model} not found.")
        
        model = self.env[self.model_id.model]
    
        eval_context = {
            # core Odoo
            "env": self.env,
            "model": model,
    
            # HTTP / API context
            "request_method": ctx.get("core_api_method"),
            "route": ctx.get("core_api_route"),
            "endpoint": ctx.get("core_api_endpoint_code"),
    
            # payload
            "body": ctx.get("core_api_body"),
            "params": ctx.get("core_api_params"),
    
            # active records (quan trọng)
            "active_model": ctx.get("active_model"),
            "active_id": ctx.get("active_id"),
            "active_ids": ctx.get("active_ids"),
    
            # optional convenience
            "application_id": ctx.get("core_api_application_id"),
        }

        safe_eval(
            self.code.strip(), 
            eval_context, 
            mode="exec")

        return eval_context.get('action', False)
