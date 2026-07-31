# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError
from odoo.addons.nsp_core.utils import new_management_code


class NspParkingArea(models.Model):
    """Server-owned parking topology and operational configuration.

    Parking topology remains on Cloud/Edge. Controllers receive only the
    technical configuration of the Readers and antenna ports they manage.
    """

    _name = "nsp.parking.area"
    _description = "NSP Parking Operation Configuration"
    _rec_name = "name"
    _order = "branch_id, name, id"

    name = fields.Char(string="Parking Area Name", required=True)
    code = fields.Char(
        string="Parking Area Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("PARK"),
    )
    branch_id = fields.Many2one(
        "nsp.branch",
        string="Branch",
        required=True,
        ondelete="restrict",
        index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft / Configuring"),
            ("operational", "Operational"),
            ("maintenance", "Maintenance"),
            ("blocked", "Blocked"),
        ],
        string="State",
        default="draft",
        required=True,
        index=True,
    )
    lane_ids = fields.One2many(
        "nsp.parking.lane", "parking_area_id", string="Parking Lanes"
    )
    antenna_transition_ids = fields.Many2many(
        "nsp.parking.antenna.transition",
        string="Antenna Movement Rules",
        compute="_compute_topology",
    )
    controller_ids = fields.Many2many(
        "nsp.controller",
        string="Controllers",
        compute="_compute_topology",
        search="_search_controllers",
        help="Controllers assigned to active parking lanes.",
    )
    reader_ids = fields.Many2many(
        "nsp.device",
        string="Readers",
        compute="_compute_topology",
        help="Readers whose antenna ports are mapped to this parking operation.",
    )
    antenna_ids = fields.Many2many(
        "nsp.device.antenna",
        string="Antennas",
        compute="_compute_topology",
        help="Reader antenna ports mapped to this parking operation.",
    )

    controller_count = fields.Integer(compute="_compute_counts")
    reader_count = fields.Integer(compute="_compute_counts")
    antenna_count = fields.Integer(compute="_compute_counts")
    lane_count = fields.Integer(compute="_compute_counts")
    whitelist_count = fields.Integer(compute="_compute_whitelist_count")

    _sql_constraints = [
        ("code_unique", "unique(code)", "Parking Area Code must be unique."),
    ]

    @api.depends(
        "lane_ids.active",
        "lane_ids.controller_id",
        "lane_ids.antenna_transition_ids.from_antenna_id",
        "lane_ids.antenna_transition_ids.from_antenna_id.device_id",
        "lane_ids.antenna_transition_ids.to_antenna_id",
        "lane_ids.antenna_transition_ids.to_antenna_id.device_id",
    )
    def _compute_topology(self):
        for rec in self:
            active_lanes = rec.lane_ids.filtered("active")
            transitions = active_lanes.mapped("antenna_transition_ids")
            antennas = transitions.mapped("from_antenna_id") | transitions.mapped("to_antenna_id")
            readers = antennas.mapped("device_id")
            controllers = active_lanes.mapped("controller_id")
            rec.antenna_transition_ids = transitions
            rec.reader_ids = readers
            rec.antenna_ids = antennas
            rec.controller_ids = controllers

    @api.model
    def _search_controllers(self, operator, value):
        return [("lane_ids.controller_id", operator, value)]

    @api.depends(
        "controller_ids",
        "reader_ids",
        "antenna_ids",
        "lane_ids.active",
    )
    def _compute_counts(self):
        for rec in self:
            rec.controller_count = len(rec.controller_ids)
            rec.reader_count = len(rec.reader_ids)
            rec.antenna_count = len(rec.antenna_ids)
            rec.lane_count = len(rec.lane_ids.filtered("active"))

    def _compute_whitelist_count(self):
        count = self.env["nsp.device.whitelist"].sudo().search_count([])
        for rec in self:
            rec.whitelist_count = count

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["code"] = self._normalize_code(
                vals.get("code") or new_management_code("PARK")
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        return super().write(values)

    def action_open_live_monitor(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "name": _("Parking Live Monitor"),
            "tag": "nsp_parking_live_monitor",
            "target": "fullscreen",
            "params": {"parking_area_id": self.id},
        }

    @api.model
    def get_live_monitor_snapshot(self, parking_area_id, limit=16):
        """Initial/reconciliation payload for the customer-facing Live Monitor."""
        if not (
            self.env.user.has_group("nsp_core.group_nsp_operator")
            or self.env.user.has_group("nsp_core.group_nsp_it_parking")
            or self.env.user.has_group("base.group_system")
        ):
            from odoo.exceptions import AccessError
            raise AccessError(_("You do not have access to the Parking Live Monitor."))

        try:
            parking_area_id = int(parking_area_id or 0)
            limit = min(max(int(limit or 12), 3), 50)
        except (TypeError, ValueError):
            parking_area_id, limit = 0, 12
        area = self.sudo().browse(parking_area_id).exists()
        if not area:
            return {"found": False}

        transactions = self.env["nsp.parking.transaction"].sudo().search(
            [
                ("lane_id.parking_area_id", "=", area.id),
                ("event_type", "=", "check_in"),
            ],
            order="event_time desc, id desc",
            limit=limit,
        )
        items = [tx._live_monitor_payload() for tx in transactions[::-1]]
        return {
            "found": True,
            "parking_area_id": area.id,
            "parking_area_name": area.name,
            "branch_name": area.branch_id.name or "",
            "state": area.state,
            "items": items,
        }


    def _lane_payload(self):
        """Return directed antenna transitions for Cloud/Edge synchronization."""
        self.ensure_one()
        result = []
        lanes = self.lane_ids.filtered("active").sorted(
            key=lambda item: (item.lane_no, item.id)
        )
        for lane in lanes:
            transitions = []
            for transition in lane.antenna_transition_ids.sorted(
                key=lambda item: (
                    item.from_serial_number or "",
                    item.from_antenna_no,
                    item.to_serial_number or "",
                    item.to_antenna_no,
                    item.id,
                )
            ):
                transitions.append({
                    "from_serial_number": transition.from_serial_number or "",
                    "from_antenna_no": int(transition.from_antenna_no or 0),
                    "to_serial_number": transition.to_serial_number or "",
                    "to_antenna_no": int(transition.to_antenna_no or 0),
                    "event_type": transition.event_type,
                    "duration_seconds": float(transition.duration_seconds or 0.0),
                })
            result.append({
                "lane_code": lane.code,
                "lane_name": lane.name,
                "lane_no": int(lane.lane_no or 0),
                "controller_code": lane.controller_id.controller_id,
                "antenna_transitions": transitions,
            })
        return result

    def _operational_issues(self):
        self.ensure_one()
        issues = []
        lanes = self.lane_ids.filtered("active")
        if not lanes:
            return [_('Configure at least one active Parking Lane.')]

        for lane in lanes:
            lane_name = lane.display_name or lane.name or _("Lane")
            controller = lane.controller_id
            if not controller:
                issues.append(_("Lane %(lane)s must have a Controller.") % {"lane": lane_name})
                continue

            controller_whitelist = controller.whitelist_id
            if (
                not controller_whitelist
                or not controller_whitelist.active
                or controller_whitelist.device_type_code != "CONTROLLER"
            ):
                issues.append(
                    _("Lane %(lane)s Controller must be an active Controller in Device Whitelist.")
                    % {"lane": lane_name}
                )
            server = controller.edge_server_id
            server_whitelist = server.whitelist_id if server else False
            if (
                not server
                or not server.active
                or server.cloud_removed
                or not server_whitelist
                or not server_whitelist.active
                or server_whitelist.device_type_code != "SERVER"
            ):
                issues.append(
                    _("Lane %(lane)s Controller must belong to the published active Server assembly.")
                    % {"lane": lane_name}
                )

            rules = lane.antenna_transition_ids
            if not rules:
                issues.append(
                    _("Lane %(lane)s must have at least one Antenna Movement Rule.")
                    % {"lane": lane_name}
                )
                continue

            wrong_scope = rules.filtered(
                lambda item: (
                    item.from_controller_id != controller
                    or item.to_controller_id != controller
                )
            )
            if wrong_scope:
                issues.append(
                    _("All antennas of lane %(lane)s must belong to Controller %(controller)s.")
                    % {
                        "lane": lane_name,
                        "controller": controller.controller_id,
                    }
                )

            invalid_antennas = rules.mapped("from_antenna_id") | rules.mapped("to_antenna_id")
            invalid_antennas = invalid_antennas.filtered(
                lambda antenna: (
                    not antenna.active
                    or not antenna.whitelist_id
                    or not antenna.whitelist_id.active
                    or antenna.whitelist_id.device_type_code != "ANTENNA"
                    or not antenna.device_id.active
                    or not antenna.device_id.whitelist_id
                    or not antenna.device_id.whitelist_id.active
                    or antenna.device_id.whitelist_id.device_type_code != "RFID_READER"
                )
            )
            if invalid_antennas:
                issues.append(
                    _("Lane %(lane)s contains an Antenna or RFID Reader that is not active in Device Whitelist.")
                    % {"lane": lane_name}
                )
        return issues

    def _open_related_action(self, action_xmlid, records, name, context=None):
        self.ensure_one()
        action = self.env.ref(action_xmlid).sudo().read()[0]
        action.update(
            {
                "name": name,
                "domain": [("id", "in", records.ids)] if records else [],
                "context": dict(context or {}),
            }
        )
        return action

    def action_open_controllers(self):
        self.ensure_one()
        context = {}
        return self._open_related_action(
            "nsp_business_gatekeeper.action_nsp_controllers",
            self.controller_ids,
            _("Controllers"),
            context,
        )

    def action_open_readers(self):
        self.ensure_one()
        context = {}
        if len(self.controller_ids) == 1:
            context["default_controller_id"] = self.controller_ids.id
        return self._open_related_action(
            "nsp_business_gatekeeper.nsp_device_action", self.reader_ids, _("Readers"), context
        )

    def action_open_antennas(self):
        self.ensure_one()
        context = {}
        if len(self.reader_ids) == 1:
            context["default_device_id"] = self.reader_ids.id
        return self._open_related_action(
            "nsp_business_gatekeeper.action_nsp_device_antenna",
            self.antenna_ids,
            _("Antennas"),
            context,
        )

    def action_open_lanes(self):
        self.ensure_one()
        action = self.env.ref("nsp_business_gatekeeper.action_nsp_parking_lane").sudo().read()[0]
        action.update(
            {
                "name": _("Parking Lanes"),
                "domain": [("parking_area_id", "=", self.id)],
                "context": {"default_parking_area_id": self.id},
            }
        )
        return action

    def action_set_operational(self):
        for rec in self:
            issues = rec._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
        self.write({"state": "operational"})
        return True

    def action_reset_to_draft(self):
        self.write({"state": "draft"})
        return True

    def action_set_maintenance(self):
        self.write({"state": "maintenance"})
        return True

    def action_set_blocked(self):
        self.write({"state": "blocked"})
        return True

    def prepare_sync_payload(self):
        self.ensure_one()
        return {
            "parking_area_code": self.code,
            "parking_area_name": self.name,
            "branch_code": self.branch_id.code or "",
            "state": self.state,
            "lanes": self._lane_payload(),
        }


class NspParkingLane(models.Model):
    _name = "nsp.parking.lane"
    _description = "NSP Parking Lane"
    _order = "parking_area_id, lane_no, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True, default="Lane")
    code = fields.Char(
        string="Lane Code",
        required=True,
        readonly=True,
        copy=False,
        index=True,
        default=lambda self: new_management_code("LANE"),
    )
    display_name = fields.Char(compute="_compute_display_name", store=True)
    parking_area_id = fields.Many2one(
        "nsp.parking.area",
        string="Parking Area",
        required=True,
        ondelete="cascade",
        index=True,
    )
    controller_id = fields.Many2one(
        "nsp.controller",
        string="Controller",
        required=True,
        ondelete="restrict",
        index=True,
        help="Controller that owns every Reader and antenna used by this Lane.",
    )
    lane_no = fields.Integer(string="Lane No.", default=1, required=True)
    active = fields.Boolean(default=True, index=True)
    antenna_transition_ids = fields.One2many(
        "nsp.parking.antenna.transition",
        "lane_id",
        string="Antenna Movement Rules",
    )
    transition_count = fields.Integer(compute="_compute_transition_count")

    _sql_constraints = [
        (
            "lane_code_per_area_unique",
            "unique(parking_area_id, code)",
            "Lane Code must be unique within a Parking Area.",
        ),
        (
            "lane_no_per_area_unique",
            "unique(parking_area_id, lane_no)",
            "Lane number must be unique within a Parking Area.",
        ),
        ("lane_no_positive", "CHECK(lane_no > 0)", "Lane number must be greater than zero."),
    ]

    @api.depends("parking_area_id.name", "name", "lane_no")
    def _compute_display_name(self):
        for rec in self:
            lane_name = rec.name or (_("Lane %s") % (rec.lane_no or ""))
            rec.display_name = "%s / %s" % (
                rec.parking_area_id.name or _("Parking"),
                lane_name,
            )

    @api.depends("antenna_transition_ids")
    def _compute_transition_count(self):
        for rec in self:
            rec.transition_count = len(rec.antenna_transition_ids)

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for source in vals_list:
            vals = dict(source)
            vals["code"] = self._normalize_code(
                vals.get("code") or new_management_code("LANE")
            )
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        values = dict(vals)
        if "code" in values:
            values["code"] = self._normalize_code(values.get("code"))
        result = super().write(values)
        if "controller_id" in values:
            invalid = self.mapped("antenna_transition_ids").filtered(
                lambda item: (
                    item.from_controller_id != item.lane_id.controller_id
                    or item.to_controller_id != item.lane_id.controller_id
                )
            )
            if invalid:
                raise ValidationError(
                    _("Existing Antenna Movement Rules do not belong to the selected Lane Controller.")
                )
        return result


class NspParkingAntennaTransition(models.Model):
    """Directed physical RFID path used by the Edge parking business engine.

    A transition is the authoritative timing rule for one movement. Lane-level
    fixed timing windows are intentionally not stored. The transition
    itself says which antenna must be observed first, which antenna comes next,
    what business event it represents, and how long that movement may take.
    """

    _name = "nsp.parking.antenna.transition"
    _description = "NSP Parking Antenna Transition"
    _order = "lane_id, event_type, from_antenna_id, to_antenna_id, id"
    _rec_name = "rule_name"

    rule_name = fields.Char(compute="_compute_rule_name")
    lane_id = fields.Many2one(
        "nsp.parking.lane",
        string="Parking Lane",
        required=True,
        ondelete="cascade",
        index=True,
    )
    parking_area_id = fields.Many2one(
        "nsp.parking.area", related="lane_id.parking_area_id", readonly=True
    )
    lane_controller_id = fields.Many2one(
        "nsp.controller", related="lane_id.controller_id", readonly=True
    )
    event_type = fields.Selection(
        [("check_in", "Check-in"), ("check_out", "Check-out")],
        string="Event Type",
        required=True,
        index=True,
        help="Business event created when the same Vehicle RFID moves from From Antenna to To Antenna within Duration.",
    )
    from_antenna_id = fields.Many2one(
        "nsp.device.antenna",
        string="From Antenna",
        required=True,
        ondelete="restrict",
        index=True,
    )
    to_antenna_id = fields.Many2one(
        "nsp.device.antenna",
        string="To Antenna",
        required=True,
        ondelete="restrict",
        index=True,
    )
    duration_seconds = fields.Float(
        string="Duration (Seconds)",
        required=True,
        default=2.0,
        digits=(8, 3),
        help="Maximum measured time allowed between From Antenna and To Antenna for the same Vehicle RFID.",
    )

    from_controller_id = fields.Many2one(
        "nsp.controller", related="from_antenna_id.device_id.controller_id", readonly=True
    )
    to_controller_id = fields.Many2one(
        "nsp.controller", related="to_antenna_id.device_id.controller_id", readonly=True
    )
    from_device_id = fields.Many2one(
        "nsp.device", related="from_antenna_id.device_id", readonly=True
    )
    to_device_id = fields.Many2one(
        "nsp.device", related="to_antenna_id.device_id", readonly=True
    )
    from_serial_number = fields.Char(
        related="from_device_id.serial_number", readonly=True
    )
    to_serial_number = fields.Char(
        related="to_device_id.serial_number", readonly=True
    )
    from_antenna_no = fields.Integer(
        related="from_antenna_id.antenna_no", readonly=True
    )
    to_antenna_no = fields.Integer(
        related="to_antenna_id.antenna_no", readonly=True
    )

    _sql_constraints = [
        (
            "unique_directed_transition",
            "unique(lane_id, from_antenna_id, to_antenna_id)",
            "The same directed Antenna Transition can be configured only once per Lane.",
        ),
        (
            "transition_duration_positive",
            "CHECK(duration_seconds > 0)",
            "Antenna Transition Duration must be greater than zero.",
        ),
        (
            "transition_antennas_different",
            "CHECK(from_antenna_id <> to_antenna_id)",
            "From Antenna and To Antenna must be different.",
        ),
    ]

    @api.depends(
        "lane_id.display_name",
        "event_type",
        "from_antenna_id.display_name",
        "to_antenna_id.display_name",
        "duration_seconds",
    )
    def _compute_rule_name(self):
        labels = dict(self._fields["event_type"].selection)
        for rec in self:
            rec.rule_name = "%s / %s: %s → %s / %.3gs" % (
                rec.lane_id.display_name or _("Lane"),
                labels.get(rec.event_type, rec.event_type or ""),
                rec.from_antenna_id.display_name or _("Antenna"),
                rec.to_antenna_id.display_name or _("Antenna"),
                rec.duration_seconds or 0.0,
            )

    @api.constrains(
        "lane_id", "from_antenna_id", "to_antenna_id", "duration_seconds"
    )
    def _check_transition(self):
        Whitelist = self.env["nsp.device.whitelist"].sudo()
        for rec in self:
            if not rec.lane_id or not rec.from_antenna_id or not rec.to_antenna_id:
                continue
            if rec.from_antenna_id == rec.to_antenna_id:
                raise ValidationError(_("From Antenna and To Antenna must be different."))
            if rec.duration_seconds <= 0:
                raise ValidationError(_("Antenna Transition Duration must be greater than zero."))
            if (
                rec.from_controller_id != rec.lane_id.controller_id
                or rec.to_controller_id != rec.lane_id.controller_id
            ):
                raise ValidationError(
                    _("Both antennas must belong to the Controller assigned to this Lane.")
                )

            serials = {
                str(rec.from_serial_number or "").strip().upper(),
                str(rec.to_serial_number or "").strip().upper(),
            }
            serials.discard("")
            allowed = set(
                Whitelist.search([
                    ("serial_number", "in", list(serials)),
                    ("active", "=", True),
                    ("device_type_code", "=", "RFID_READER"),
                ]).mapped("serial_number")
            ) if serials else set()
            if serials - allowed:
                raise ValidationError(
                    _("Both antennas must belong to Readers in Device Whitelist.")
                )

            if "whitelist_id" in rec.from_antenna_id._fields:
                invalid_antennas = (rec.from_antenna_id | rec.to_antenna_id).filtered(
                    lambda antenna: not antenna.whitelist_id or not antenna.whitelist_id.active
                )
                if invalid_antennas:
                    raise ValidationError(_("Both antennas must be active devices in Device Whitelist."))

            antenna_ids = [rec.from_antenna_id.id, rec.to_antenna_id.id]
            conflict = self.search([
                ("id", "!=", rec.id),
                ("lane_id", "!=", rec.lane_id.id),
                "|",
                ("from_antenna_id", "in", antenna_ids),
                ("to_antenna_id", "in", antenna_ids),
            ], limit=1)
            if conflict:
                raise ValidationError(
                    _("An antenna can participate in transitions of only one Parking Lane.")
                )
