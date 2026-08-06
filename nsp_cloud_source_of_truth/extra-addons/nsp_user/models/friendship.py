from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class NspUserFriendship(models.Model):
    _name = "nsp.user.friendship"
    _description = "NSP User Friendship"
    _rec_name = "name"
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    requester_id = fields.Many2one(
        "nsp.user",
        string="Requester",
        required=True,
        index=True,
        ondelete="cascade",
    )
    addressee_id = fields.Many2one(
        "nsp.user",
        string="Friend",
        required=True,
        index=True,
        ondelete="cascade",
    )
    pair_key = fields.Char(required=True, copy=False, readonly=True, index=True)
    state = fields.Selection(
        [("pending", "Pending"), ("accepted", "Accepted")],
        required=True,
        readonly=True,
        default="pending",
        index=True,
    )
    accepted_at = fields.Datetime(readonly=True, index=True)

    _pair_unique = models.Constraint(
        "UNIQUE(pair_key)",
        "A friendship already exists between these users.",
    )

    def init(self):
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_user_friendship_requester_state_idx
                ON nsp_user_friendship (requester_id, state, id DESC)
            """
        )
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS nsp_user_friendship_addressee_state_idx
                ON nsp_user_friendship (addressee_id, state, id DESC)
            """
        )

    @api.depends("requester_id.name", "addressee_id.name")
    def _compute_name(self):
        for record in self:
            record.name = "%s ↔ %s" % (
                record.requester_id.name or _("User"),
                record.addressee_id.name or _("User"),
            )

    @staticmethod
    def _make_pair_key(user_a_id, user_b_id):
        first_id, second_id = sorted((int(user_a_id), int(user_b_id)))
        return f"{first_id}:{second_id}"

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            requester_id = int(values.get("requester_id") or 0)
            addressee_id = int(values.get("addressee_id") or 0)
            if not requester_id or not addressee_id:
                raise ValidationError(_("Requester and Friend are required."))
            if requester_id == addressee_id:
                raise ValidationError(_("A user cannot add themselves as a friend."))
            values.update(
                {
                    "pair_key": self._make_pair_key(requester_id, addressee_id),
                    "state": "pending",
                    "accepted_at": False,
                }
            )
            prepared.append(values)
        return super().create(prepared)

    @api.constrains("requester_id", "addressee_id")
    def _check_users(self):
        for record in self:
            if record.requester_id == record.addressee_id:
                raise ValidationError(_("A user cannot add themselves as a friend."))

    def action_accept(self):
        self.filtered(lambda record: record.state == "pending").write(
            {"state": "accepted", "accepted_at": fields.Datetime.now()}
        )
        return True

    def action_cancel(self):
        self.unlink()
        return True
