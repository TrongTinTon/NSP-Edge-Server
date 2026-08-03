from odoo import fields, models


class NspUser(models.Model):
    _inherit = "nsp.user"

    odoo_user_id = fields.Many2one(
        "res.users",
        string="Odoo Web Account",
        required=False,
        ondelete="set null",
        index=True,
        copy=False,
    )

    def _auto_init(self):
        result = super()._auto_init()
        self.env.cr.execute(
            "ALTER TABLE IF EXISTS nsp_user "
            "ALTER COLUMN odoo_user_id DROP NOT NULL"
        )
        return result
