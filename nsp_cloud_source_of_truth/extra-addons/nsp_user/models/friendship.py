from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


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

    @api.model
    def _is_friendship_admin(self):
        return self.env["nsp.user"]._is_friendship_admin()

    @api.model
    def _current_identity(self, required=True):
        return self.env["nsp.user"]._current_nsp_identity(required=required)

    def _check_self_relationship_access(self, accept_only=False):
        if self._is_friendship_admin():
            return True
        identity = self._current_identity(required=True)
        for record in self:
            if accept_only:
                allowed = record.addressee_id == identity
            else:
                allowed = identity in (record.requester_id, record.addressee_id)
            if not allowed:
                raise AccessError(_("You can manage only your own friendship requests."))
        return True

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
            if not self._is_friendship_admin():
                identity = self._current_identity(required=True)
                if requester_id != identity.id:
                    raise AccessError(_("Friend requests must be sent from your own NSP identity."))
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

    def write(self, vals):
        if not self._is_friendship_admin() and not self.env.context.get(
            "nsp_friendship_action"
        ):
            raise AccessError(_("Use the friendship actions to change a relationship."))
        return super().write(vals)

    def unlink(self):
        self._check_self_relationship_access(accept_only=False)
        return super().unlink()

    def action_accept(self):
        self._check_self_relationship_access(accept_only=True)
        self.filtered(lambda record: record.state == "pending").with_context(
            nsp_friendship_action=True
        ).write({"state": "accepted", "accepted_at": fields.Datetime.now()})
        return True

    def action_cancel(self):
        self._check_self_relationship_access(accept_only=False)
        self.unlink()
        return True

    @api.model
    def accepted_friends_map(self, users):
        """Return accepted friend IDs for each User with one batched query.

        This is the stable service used by Vehicle Borrow validation; it does not
        bypass lending authorization and returns only accepted relationships.
        """
        users = users.exists()
        result = {user_id: [] for user_id in users.ids}
        if not users:
            return result

        user_ids = set(users.ids)
        friendships = self.sudo().search(
            [
                ("state", "=", "accepted"),
                "|",
                ("requester_id", "in", list(user_ids)),
                ("addressee_id", "in", list(user_ids)),
            ]
        )
        mapped = {user_id: set() for user_id in user_ids}
        for friendship in friendships:
            requester_id = friendship.requester_id.id
            addressee_id = friendship.addressee_id.id
            if requester_id in user_ids:
                mapped[requester_id].add(addressee_id)
            if addressee_id in user_ids:
                mapped[addressee_id].add(requester_id)
        for user_id in result:
            result[user_id] = sorted(mapped.get(user_id, set()))
        return result
