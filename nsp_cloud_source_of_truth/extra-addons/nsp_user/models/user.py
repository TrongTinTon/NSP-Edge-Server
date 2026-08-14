from collections import defaultdict

from odoo import _, api, fields, models
from odoo.addons.nsp_core.utils import new_management_code
from odoo.exceptions import AccessError, ValidationError


def _normalize_code(value):
    return str(value or "").strip().upper()


def _normalize_email(value):
    return str(value or "").strip().lower() or False


def _normalize_phone(value):
    return str(value or "").strip() or False


class NspUser(models.Model):
    _name = "nsp.user"
    _description = "NSP User"
    _inherit = ["mail.thread", "image.mixin"]
    _rec_name = "name"
    _order = "name, id"

    user_code = fields.Char(
        string="Technical Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("USER"),
        help="Stable system-generated identifier used for Cloud/Edge synchronization.",
    )
    name = fields.Char(string="User Name", required=True, tracking=True, index=True)
    active = fields.Boolean(default=True, tracking=True, index=True)
    email = fields.Char(string="Email", index=True)
    phone = fields.Char(string="Phone", index=True)
    odoo_user_id = fields.Many2one(
        "res.users",
        string="Odoo User",
        copy=False,
        tracking=True,
        ondelete="set null",
        domain=[("active", "=", True), ("share", "=", False)],
        groups="base.group_system",
        help=(
            "Optional internal Odoo account used only when this business identity "
            "needs Web access."
        ),
    )

    friendship_sent_ids = fields.One2many(
        "nsp.user.friendship",
        "requester_id",
        string="Sent Friend Requests",
    )
    friendship_received_ids = fields.One2many(
        "nsp.user.friendship",
        "addressee_id",
        string="Received Friend Requests",
    )
    accepted_friendship_ids = fields.Many2many(
        "nsp.user.friendship",
        compute="_compute_accepted_friendships",
        string="Accepted Friendships",
    )

    is_current_identity = fields.Boolean(
        compute="_compute_self_service_state",
        string="Current Identity",
    )
    can_edit_profile = fields.Boolean(
        compute="_compute_self_service_state",
        string="Can Edit Profile",
    )
    can_manage_friendships = fields.Boolean(
        compute="_compute_self_service_state",
        string="Can Manage Friendships",
    )
    can_send_friend_request = fields.Boolean(
        compute="_compute_self_service_state",
        string="Can Send Friend Request",
    )

    _user_code_unique = models.Constraint(
        "UNIQUE(user_code)",
        "User Technical Code must be unique.",
    )
    _odoo_user_unique = models.Constraint(
        "UNIQUE(odoo_user_id)",
        "An Odoo User can be linked to only one NSP User.",
    )

    @api.model
    def _current_nsp_identity(self, required=False):
        identity = self.sudo().search(
            [("odoo_user_id", "=", self.env.user.id)],
            limit=1,
        )
        if required and not identity:
            raise AccessError(
                _(
                    "Your Odoo account is not linked to an NSP User identity. "
                    "Please contact the IT Parking Admin."
                )
            )
        return identity

    @api.model
    def _is_user_master_admin(self):
        return bool(
            self.env.is_superuser()
            or self.env.user.has_group("base.group_system")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("nsp_core.group_nsp_hr_parking")
        )

    @api.model
    def _is_friendship_admin(self):
        return bool(
            self.env.is_superuser()
            or self.env.user.has_group("base.group_system")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
        )

    @api.depends("active", "odoo_user_id")
    def _compute_self_service_state(self):
        current_identity = self._current_nsp_identity(required=False)
        friendship_admin = self._is_friendship_admin()
        user_admin = self._is_user_master_admin()
        current_id = current_identity.id if current_identity else 0

        pair_keys = {}
        for record in self:
            record_id = record.id if isinstance(record.id, int) else 0
            if current_id and record_id and record_id != current_id:
                pair_keys[record_id] = self.env["nsp.user.friendship"]._make_pair_key(
                    current_id, record_id
                )

        existing_pairs = set()
        if pair_keys:
            existing_pairs = set(
                self.env["nsp.user.friendship"]
                .sudo()
                .search([("pair_key", "in", list(pair_keys.values()))])
                .mapped("pair_key")
            )

        for record in self:
            record_id = record.id if isinstance(record.id, int) else 0
            is_current = bool(current_id and record_id == current_id)
            record.is_current_identity = is_current
            record.can_edit_profile = bool(is_current or user_admin)
            record.can_manage_friendships = bool(is_current or friendship_admin)
            record.can_send_friend_request = bool(
                current_id
                and record_id
                and record_id != current_id
                and record.active
                and pair_keys.get(record_id) not in existing_pairs
            )

    def action_send_friend_request(self):
        current_identity = self._current_nsp_identity(required=True)
        Friendship = self.env["nsp.user.friendship"]
        for target in self:
            if target == current_identity:
                raise ValidationError(_("You cannot add yourself as a friend."))
            if not target.active:
                raise ValidationError(_("Archived users cannot receive friend requests."))
            Friendship.create(
                {
                    "requester_id": current_identity.id,
                    "addressee_id": target.id,
                }
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Friend Request"),
                "message": _("Friend request sent."),
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    @api.depends(
        "friendship_sent_ids.state",
        "friendship_received_ids.state",
        "friendship_sent_ids.accepted_at",
        "friendship_received_ids.accepted_at",
    )
    def _compute_accepted_friendships(self):
        persisted_ids = [record.id for record in self if isinstance(record.id, int)]
        friendship_ids_by_user = defaultdict(list)

        if persisted_ids:
            friendships = self.env["nsp.user.friendship"].sudo().search(
                [
                    ("state", "=", "accepted"),
                    "|",
                    ("requester_id", "in", persisted_ids),
                    ("addressee_id", "in", persisted_ids),
                ],
                order="accepted_at desc, id desc",
            )
            requested_ids = set(persisted_ids)
            for friendship in friendships:
                if friendship.requester_id.id in requested_ids:
                    friendship_ids_by_user[friendship.requester_id.id].append(
                        friendship.id
                    )
                if friendship.addressee_id.id in requested_ids:
                    friendship_ids_by_user[friendship.addressee_id.id].append(
                        friendship.id
                    )

        Friendship = self.env["nsp.user.friendship"]
        for record in self:
            record.accepted_friendship_ids = Friendship.browse(
                friendship_ids_by_user.get(record.id, [])
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            values = dict(source)
            values["user_code"] = _normalize_code(
                values.get("user_code") or new_management_code("USER")
            )
            if "email" in values:
                values["email"] = _normalize_email(values.get("email"))
            if "phone" in values:
                values["phone"] = _normalize_phone(values.get("phone"))
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if not self._is_user_master_admin():
            forbidden = {"active", "odoo_user_id", "user_code"}.intersection(values)
            if forbidden:
                raise AccessError(
                    _("Only HR Parking Officer or IT Parking Admin can change system identity fields.")
                )

        if "user_code" in values:
            normalized_code = _normalize_code(values.get("user_code"))
            if any(
                record.user_code and record.user_code != normalized_code
                for record in self
            ):
                raise ValidationError(
                    _("User Technical Code cannot be changed after creation.")
                )
            values["user_code"] = normalized_code
        if "email" in values:
            values["email"] = _normalize_email(values.get("email"))
        if "phone" in values:
            values["phone"] = _normalize_phone(values.get("phone"))
        return super().write(values)

    @api.constrains("odoo_user_id")
    def _check_odoo_user(self):
        for record in self:
            if record.odoo_user_id and record.odoo_user_id.share:
                raise ValidationError(
                    _("Odoo User must be an internal user, not a portal or public user.")
                )

    def action_archive(self):
        if not self._is_user_master_admin():
            raise AccessError(_("Only HR Parking Officer or IT Parking Admin can archive users."))
        self.filtered("active").write({"active": False})
        return True

    def action_unarchive(self):
        if not self._is_user_master_admin():
            raise AccessError(_("Only HR Parking Officer or IT Parking Admin can restore users."))
        self.filtered(lambda record: not record.active).write({"active": True})
        return True
