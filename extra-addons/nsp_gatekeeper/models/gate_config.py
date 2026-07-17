# -*- coding: utf-8 -*-
import hashlib
import json

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError


class NspGate(models.Model):
    _name = "nsp.gate"
    _description = "NSP Gate"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "name"
    _order = "branch_id, name, id"

    name = fields.Char(string="Gate Name", required=True, tracking=True)
    code = fields.Char(string="Gate Code", required=True, tracking=True, copy=False, index=True)
    branch_id = fields.Many2one("nsp.branch", string="Branch", required=True, ondelete="restrict", tracking=True, index=True)
    controller_ids = fields.Many2many(
        "nsp.controller", "nsp_gate_controller_rel", "gate_id", "controller_id",
        string="Controllers", tracking=True,
        help="All controllers participating in this gate. No primary controller is used."
    )
    gate_status = fields.Selection([
        ("active", "Active"),
        ("blocked", "Blocked"),
        ("maintenance", "Maintenance"),
    ], string="Status", default="active", required=True, tracking=True, index=True)
    operation_state = fields.Selection([
        ("draft", "Draft / Configuring"),
        ("operational", "Operational"),
    ], string="Operation State", default="draft", required=True, tracking=True, index=True)
    detection_window_ms = fields.Integer(string="Detection Window (ms)", default=1500)
    sequence_required = fields.Boolean(string="Require Antenna Sequence", default=True)
    entry_requires_user_tid = fields.Boolean(string="Entry Requires User TID", default=False)
    exit_requires_user_tid = fields.Boolean(string="Exit Requires User TID", default=True)

    lane_ids = fields.One2many("nsp.gate.lane", "gate_id", string="Lanes")
    lane_antenna_group_ids = fields.One2many("nsp.gate.lane.antenna.group", "gate_id", string="Antenna Groups")
    lane_antenna_rule_ids = fields.One2many("nsp.gate.lane.antenna.mapping", "gate_id", string="Antenna Mapping")
    controller_count = fields.Integer(string="Controllers", compute="_compute_configuration_counts")
    lane_count = fields.Integer(string="Lanes", compute="_compute_configuration_counts")
    entry_lane_count = fields.Integer(string="Entry Lanes", compute="_compute_configuration_counts")
    exit_lane_count = fields.Integer(string="Exit Lanes", compute="_compute_configuration_counts")
    both_lane_count = fields.Integer(string="Two-way Lanes", compute="_compute_configuration_counts")
    entry_antenna_count = fields.Integer(string="Entry-capable Antennas", compute="_compute_rule_counts")
    exit_antenna_count = fields.Integer(string="Exit-capable Antennas", compute="_compute_rule_counts")
    invalid_antenna_count = fields.Integer(string="Invalid Mapping", compute="_compute_rule_counts")

    config_revision = fields.Integer(string="Config Revision", default=1, readonly=True, copy=False, tracking=True)
    config_hash = fields.Char(string="Current Config Hash", readonly=True, copy=False)
    applied_config_revision = fields.Integer(string="Applied Revision", readonly=True, copy=False)
    applied_config_hash = fields.Char(string="Applied Config Hash", readonly=True, copy=False)
    config_state = fields.Selection([
        ("draft", "Not Synced"),
        ("pending_sync", "Waiting Controller"),
        ("applied", "Synced"),
        ("error", "Sync Error"),
    ], string="Controller Sync", default="draft", readonly=True, copy=False, tracking=True)
    last_synced_at = fields.Datetime(string="Last Sent to Controller", readonly=True, copy=False)
    last_applied_at = fields.Datetime(string="Last Applied at Server", readonly=True, copy=False)
    controller_applied_at = fields.Datetime(string="Controller Applied At", readonly=True, copy=False)
    apply_message = fields.Text(string="Apply Message", readonly=True, copy=False)

    _sql_constraints = [
        ("code_unique", "unique(code)", "Gate Code must be unique."),
        ("detection_window_positive", "CHECK(detection_window_ms >= 0)", "Detection Window must be positive."),
    ]

    @api.depends("controller_ids", "lane_ids", "lane_ids.lane_type", "lane_ids.direction")
    def _compute_configuration_counts(self):
        for gate in self:
            gate.controller_count = len(gate.controller_ids)
            active_lanes = gate.lane_ids.filtered(lambda l: l.active)
            gate.lane_count = len(active_lanes)
            gate.entry_lane_count = len(active_lanes.filtered(lambda l: l.lane_type == "one_way" and l.direction == "entry"))
            gate.exit_lane_count = len(active_lanes.filtered(lambda l: l.lane_type == "one_way" and l.direction == "exit"))
            gate.both_lane_count = len(active_lanes.filtered(lambda l: l.lane_type == "two_way"))

    @api.depends("lane_antenna_rule_ids.is_active", "lane_antenna_rule_ids.effective_direction", "lane_antenna_rule_ids.lane_id.active")
    def _compute_rule_counts(self):
        for gate in self:
            rules = gate._valid_lane_rules()
            gate.entry_antenna_count = len(rules.filtered(lambda r: (r.effective_direction or r.lane_direction) in ("entry", "both")))
            gate.exit_antenna_count = len(rules.filtered(lambda r: (r.effective_direction or r.lane_direction) in ("exit", "both")))
            gate.invalid_antenna_count = len(gate._invalid_lane_rules())

    @api.model_create_multi
    def create(self, vals_list):
        Branch = self.env["nsp.branch"].sudo()
        default_branch = Branch.get_default_branch() if hasattr(Branch, "get_default_branch") else Branch.search([], limit=1)
        for vals in vals_list:
            if not vals.get("branch_id") and default_branch:
                vals["branch_id"] = default_branch.id
            if vals.get("code"):
                vals["code"] = self._normalize_code(vals["code"])
        records = super().create(vals_list)
        records._refresh_config_hash(bump_if_changed=False)
        return records

    def write(self, vals):
        vals = dict(vals)
        if vals.get("code"):
            vals["code"] = self._normalize_code(vals["code"])
        res = super().write(vals)
        watched = {
            "name", "code", "branch_id", "gate_status", "operation_state", "controller_ids",
            "detection_window_ms", "sequence_required", "entry_requires_user_tid", "exit_requires_user_tid",
        }
        if watched.intersection(vals.keys()):
            self._refresh_config_hash(bump_if_changed=True)
        return res

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    def _branch_timezone(self):
        self.ensure_one()
        return (self.branch_id.timezone if self.branch_id else False) or "Asia/Ho_Chi_Minh"

    def _controller_set(self):
        self.ensure_one()
        return self.controller_ids

    def _has_controller(self, controller):
        self.ensure_one()
        return bool(controller and controller in self.controller_ids)

    @api.model
    def _controller_gate_domain(self, controller, extra_domain=None):
        controller_id = controller.id if hasattr(controller, "id") else int(controller or 0)
        domain = [("controller_ids", "in", [controller_id])]
        if extra_domain:
            domain = ["&"] + domain + list(extra_domain)
        return domain

    def _valid_lane_rules(self, for_controller=False):
        self.ensure_one()
        rules = self.lane_antenna_rule_ids.filtered(lambda r: r.is_active and r.lane_id.active and r.antenna_ref_id)
        if for_controller:
            rules = rules.filtered(lambda r: r.controller_id == for_controller)
        return rules

    def _valid_lane_rules_for_direction(self, direction, for_controller=False):
        self.ensure_one()
        return self._valid_lane_rules(for_controller=for_controller).filtered(
            lambda r: (r.effective_direction or r.lane_direction) in (direction, "both")
        )

    def _rules_for_direction(self, direction):
        self.ensure_one()
        return self._valid_lane_rules_for_direction(direction).sorted(key=lambda r: (r.sequence_order or 0, r.device_id.id or 0, r.antenna_id or 0))

    def _lane_rules_for_log(self, lane=False, direction=False):
        self.ensure_one()
        rules = self._valid_lane_rules()
        if lane:
            rules = rules.filtered(lambda r: r.lane_id == lane)
        if direction in ("entry", "exit"):
            rules = rules.filtered(lambda r: (r.effective_direction or r.lane_direction) in (direction, "both"))
        return rules.sorted(key=lambda r: (r.sequence_order or 0, r.device_id.id or 0, r.antenna_id or 0))

    def _invalid_lane_rules(self):
        self.ensure_one()
        return self.lane_antenna_rule_ids.filtered(
            lambda r: r.is_active and (not r.lane_id or not r.lane_id.active or not r.antenna_ref_id or r.gate_id != self)
        )

    def _lane_has_direction_rule(self, lane, direction):
        rules = lane.antenna_rule_ids.filtered(lambda r: r.is_active and r.antenna_ref_id)
        return bool(rules.filtered(lambda r: (r.effective_direction or r.lane_direction) in (direction, "both")))

    def _lane_has_tag_role_rule(self, lane, tag_role):
        rules = lane.antenna_rule_ids.filtered(lambda r: r.is_active and r.antenna_ref_id)
        if tag_role == "vehicle_tid":
            return bool(rules.filtered(lambda r: (r.tag_role or "vehicle_tid") in ("vehicle_tid", "both")))
        if tag_role == "user_tid":
            return bool(rules.filtered(lambda r: (r.tag_role or "vehicle_tid") in ("user_tid", "both")))
        return False

    def _controller_codes_payload(self):
        self.ensure_one()
        return [controller.controller_id for controller in self.controller_ids if controller.controller_id]

    def _lane_payload(self, for_controller=False):
        """Return Lane → Antenna Group → Mapping without database IDs.

        Physical antennas are identified only by serial_number + antenna_no.
        """
        self.ensure_one()
        lanes_payload = []
        for lane in self.lane_ids.filtered(lambda rec: rec.active).sorted(key=lambda rec: (rec.sequence or 0, rec.lane_no or 0, rec.id)):
            groups_payload = []
            groups = lane.antenna_group_ids.filtered(lambda rec: rec.active).sorted(key=lambda rec: (rec.sequence or 0, rec.direction, rec.id))
            for group in groups:
                mappings = group.antenna_mapping_ids.filtered(
                    lambda rec: rec.is_active and rec.antenna_ref_id and rec.device_id and rec.device_id.managed
                )
                if for_controller:
                    mappings = mappings.filtered(lambda rec: rec.controller_id == for_controller)
                if not mappings:
                    continue
                antennas = []
                for mapping in mappings.sorted(key=lambda rec: (rec.sequence_no or 0, rec.serial_number or "", rec.antenna_no or 0, rec.id)):
                    antenna = {
                        "serial_number": mapping.serial_number or "",
                        "antenna_no": int(mapping.antenna_no or 0),
                    }
                    if group.detection_mode == "sequential":
                        antenna["sequence_no"] = int(mapping.sequence_no or 0)
                    antennas.append(antenna)
                groups_payload.append({
                    "direction": group.direction,
                    "detection_mode": group.detection_mode,
                    "antennas": antennas,
                })
            if for_controller and not groups_payload:
                continue
            lanes_payload.append({
                "lane_code": lane.code,
                "lane_type": lane.lane_type,
                "detection_window_ms": int(lane.detection_window_ms or self.detection_window_ms or 0),
                "required_vehicle_tid": bool(lane.required_vehicle_tid),
                "required_user_tid": bool(lane.required_user_tid),
                "antenna_groups": groups_payload,
            })
        return lanes_payload

    def _canonical_config(self):
        self.ensure_one()
        return {
            "branch_code": self.branch_id.code if self.branch_id else False,
            "gate_code": self.code,
            "operational": self.gate_status == "active" and self.operation_state == "operational",
            "controller_codes": self._controller_codes_payload(),
            "lanes": self._lane_payload(),
        }

    def _compute_config_hash_value(self):
        self.ensure_one()
        raw = json.dumps(self._canonical_config(), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _refresh_config_hash(self, bump_if_changed=False):
        for gate in self:
            new_hash = gate._compute_config_hash_value()
            vals = {}
            mark_pending = False
            if gate.config_hash != new_hash:
                vals["config_hash"] = new_hash
                if bump_if_changed:
                    vals["config_revision"] = (gate.config_revision or 0) + 1
                    vals["config_state"] = "draft"
                    vals["apply_message"] = False
                    mark_pending = True
            if vals:
                super(NspGate, gate).write(vals)
                if mark_pending:
                    gate._mark_nsp_sync_pending(_("Gate changed. Waiting for controller to pull and apply."))

    def _nsp_sync_record_model(self):
        return self.env["nsp.sync.record"].sudo() if "nsp.sync.record" in self.env.registry.models else False

    def _mark_nsp_sync_pending(self, message=False):
        Record = self._nsp_sync_record_model()
        if not Record:
            return False
        for gate in self:
            for controller in gate.controller_ids:
                Record.mark_pending(
                    controller=controller,
                    action_code="nsp_gatekeeper_gate_config_sync",
                    action_name="NSP Gatekeeper Gate Config Sync",
                    record=gate,
                    record_key=gate.code,
                    message=message or _("Gate is waiting for controller sync."),
                    operation="pull",
                )
        return True

    def _mark_nsp_sync_result(self, status="synced", message=False):
        Record = self._nsp_sync_record_model()
        if not Record:
            return False
        for gate in self:
            for controller in gate.controller_ids:
                Record.mark_result(
                    controller=controller,
                    action_code="nsp_gatekeeper_gate_config_sync",
                    action_name="NSP Gatekeeper Gate Config Sync",
                    record=gate,
                    record_key=gate.code,
                    status=status,
                    message=message or gate.apply_message or False,
                    last_synced_at=gate.last_applied_at or gate.controller_applied_at or gate.last_synced_at,
                    operation="pull",
                )
        return True

    def mark_sent_to_controller(self, message=False):
        now = fields.Datetime.now()
        for gate in self:
            vals = {
                "last_synced_at": now,
                "config_state": "pending_sync",
                "apply_message": message or _("Configuration sent to Controller. Waiting for apply confirmation."),
            }
            gate.write(vals)
            gate._mark_nsp_sync_pending(vals["apply_message"])
        return True

    def mark_applied_from_controller(self, status="applied", revision=False, config_hash=False, applied_at=False, message=False):
        self.ensure_one()
        status = str(status or "applied").lower()
        ok = status in ("applied", "synced", "success", "ok")
        vals = {
            "controller_applied_at": fields.Datetime.to_datetime(applied_at) if applied_at else fields.Datetime.now(),
            "apply_message": message or (_("Controller applied Gate Config.") if ok else _("Controller failed to apply Gate Config.")),
        }
        if ok:
            vals.update({
                "config_state": "applied",
                "applied_config_revision": int(revision or self.config_revision or 0),
                "applied_config_hash": config_hash or self.config_hash,
                "last_applied_at": fields.Datetime.now(),
            })
            self.write(vals)
            self._mark_nsp_sync_result("synced", vals["apply_message"])
            return True, vals["apply_message"]
        vals["config_state"] = "error"
        self.write(vals)
        self._mark_nsp_sync_result("failed", vals["apply_message"])
        return False, vals["apply_message"]

    def action_mark_ready_to_sync(self):
        for gate in self:
            gate._refresh_config_hash(bump_if_changed=True)
            gate.write({"config_state": "draft"})
            gate._mark_nsp_sync_pending(_("Gate marked Not Synced from Odoo UI."))
        return True

    def action_reset_to_draft(self):
        self.write({"config_state": "draft", "operation_state": "draft", "apply_message": False})
        return True

    def action_load_lane_antennas_from_controllers(self):
        for gate in self:
            if not gate.controller_ids:
                raise UserError(_("Select at least one Controller first."))
            lane = gate.lane_ids.filtered(lambda rec: rec.active)[:1]
            if not lane:
                lane = self.env["nsp.gate.lane"].sudo().create({
                    "gate_id": gate.id, "name": "Lane 1", "code": "LANE-1",
                    "lane_no": 1, "lane_type": "one_way", "direction": "entry", "sequence": 10,
                })
            group = lane.antenna_group_ids.filtered(lambda rec: rec.active and rec.direction == (lane.direction if lane.lane_type == "one_way" else "entry"))[:1]
            if not group:
                group = self.env["nsp.gate.lane.antenna.group"].sudo().create({
                    "lane_id": lane.id,
                    "direction": lane.direction if lane.lane_type == "one_way" else "entry",
                    "detection_mode": "sequential",
                })
            if len(gate.controller_ids) != 1:
                raise UserError(_(
                    "Automatic antenna loading requires exactly one Gate Controller. "
                    "For a multi-controller Gate, configure each Lane Antenna Group manually so one Controller owns the complete Lane detection rule."
                ))
            Antenna = self.env["nsp.device.antenna"].sudo()
            antennas = Antenna.search([
                ("device_id.controller_id", "=", gate.controller_ids.id),
                ("device_id.managed", "=", True),
                ("is_active", "=", True),
            ], order="device_id, antenna_id")
            if not antennas:
                raise UserError(_("No enabled physical antennas were reported by the selected Gate Controllers."))
            existing = set(group.antenna_mapping_ids.mapped("antenna_ref_id").ids)
            sequence_no = len(existing) + 1
            for antenna in antennas:
                if antenna.id in existing:
                    continue
                self.env["nsp.gate.lane.antenna.mapping"].sudo().create({
                    "antenna_group_id": group.id,
                    "antenna_ref_id": antenna.id,
                    "sequence_no": sequence_no,
                    "is_active": True,
                    "note": _("Loaded from Controller enabled antenna inventory"),
                })
                sequence_no += 1
            gate._refresh_config_hash(bump_if_changed=True)
        return True

    def _operational_issues(self):
        """Return deterministic validation issues for the target Lane model."""
        self.ensure_one()
        issues = []
        active_lanes = self.lane_ids.filtered(lambda rec: rec.active)
        if not active_lanes:
            issues.append(_("Configure at least one active Lane."))
        for lane in active_lanes:
            lane_name = lane.display_name or lane.code
            groups = lane.antenna_group_ids.filtered(lambda rec: rec.active)
            directions = groups.mapped("direction")
            if lane.lane_type == "one_way":
                if lane.direction not in ("entry", "exit"):
                    issues.append(_("One-way lane %(lane)s must use entry or exit direction.") % {"lane": lane_name})
                if len(groups) != 1 or (groups and groups.direction != lane.direction):
                    issues.append(_("One-way lane %(lane)s must have exactly one antenna group matching its direction.") % {"lane": lane_name})
            elif lane.lane_type == "two_way":
                if len(groups) != 2 or set(directions) != {"entry", "exit"}:
                    issues.append(_("Two-way lane %(lane)s must have exactly one entry group and one exit group.") % {"lane": lane_name})
            else:
                issues.append(_("Lane %(lane)s has an invalid lane_type.") % {"lane": lane_name})

            any_keys_by_direction = {}
            lane_controllers = self.env["nsp.controller"].browse()
            for group in groups:
                mappings = group.antenna_mapping_ids.filtered(lambda rec: rec.is_active and rec.antenna_ref_id)
                if not mappings:
                    issues.append(_("Antenna group %(direction)s of lane %(lane)s has no enabled antenna mapping.") % {"direction": group.direction, "lane": lane_name})
                    continue
                group_controllers = mappings.mapped("controller_id")
                lane_controllers |= group_controllers
                if len(group_controllers) != 1:
                    issues.append(_(
                        "Antenna group %(direction)s of lane %(lane)s must be handled by exactly one Controller; multiple RFID Readers under that Controller are supported."
                    ) % {"direction": group.direction, "lane": lane_name})
                keys = [(rec.serial_number, int(rec.antenna_no or 0)) for rec in mappings]
                if len(keys) != len(set(keys)):
                    issues.append(_("Antenna group %(direction)s of lane %(lane)s contains duplicate physical antennas.") % {"direction": group.direction, "lane": lane_name})
                for mapping in mappings:
                    if not mapping.device_id.managed or not mapping.antenna_ref_id.is_active:
                        issues.append(_("Mapped antenna %(serial)s:%(antenna)s must belong to an active managed device and be enabled.") % {"serial": mapping.serial_number, "antenna": mapping.antenna_no})
                if group.detection_mode == "sequential":
                    sequence = sorted(int(rec.sequence_no or 0) for rec in mappings)
                    if len(mappings) < 2:
                        issues.append(_("Sequential group %(direction)s of lane %(lane)s requires at least two antennas.") % {"direction": group.direction, "lane": lane_name})
                    if sequence != list(range(1, len(sequence) + 1)):
                        issues.append(_("Sequential group %(direction)s of lane %(lane)s must use continuous sequence_no starting from 1.") % {"direction": group.direction, "lane": lane_name})
                elif group.detection_mode == "any":
                    if any(int(rec.sequence_no or 0) for rec in mappings):
                        issues.append(_("Any-mode group %(direction)s of lane %(lane)s must not define sequence_no.") % {"direction": group.direction, "lane": lane_name})
                    any_keys_by_direction[group.direction] = set(keys)
                else:
                    issues.append(_("Antenna group %(direction)s of lane %(lane)s has invalid detection_mode.") % {"direction": group.direction, "lane": lane_name})
            if len(lane_controllers) > 1:
                issues.append(_(
                    "All Antenna Groups of lane %(lane)s must be handled by the same Controller because direction detection is executed at the Controller."
                ) % {"lane": lane_name})
            if lane.lane_type == "two_way" and any_keys_by_direction.get("entry") and any_keys_by_direction.get("exit"):
                if any_keys_by_direction["entry"].intersection(any_keys_by_direction["exit"]):
                    issues.append(_("Two-way any-mode lane %(lane)s cannot reuse the same physical antenna for entry and exit.") % {"lane": lane_name})
        return issues

    def _check_operational_ready(self):
        for gate in self:
            issues = gate._operational_issues()
            if issues:
                raise UserError("\n".join(issues))
        return True

    def action_set_operational(self):
        self._check_operational_ready()
        for gate in self:
            gate._refresh_config_hash(bump_if_changed=True)
            message = _("Operational. Waiting for controller to pull and apply Gate.")
            gate.write({"operation_state": "operational", "config_state": "draft", "apply_message": message})
            gate._mark_nsp_sync_pending(message)
        return True

    def action_force_applied(self):
        self._check_operational_ready()
        for gate in self:
            gate._refresh_config_hash(bump_if_changed=False)
            gate.write({
                "applied_config_revision": gate.config_revision,
                "applied_config_hash": gate.config_hash,
                "config_state": "applied",
                "last_applied_at": fields.Datetime.now(),
                "apply_message": _("Forced as applied from Odoo UI."),
            })
            gate._mark_nsp_sync_result("synced", gate.apply_message)
        return True

    def _gate_payload(self, for_controller=False, runtime=False):
        self.ensure_one()
        if runtime:
            self._check_operational_ready()
        self._refresh_config_hash(bump_if_changed=False)
        payload = {
            "branch_code": self.branch_id.code or "",
            "gate_code": self.code,
            "operational": self.gate_status == "active" and self.operation_state == "operational",
            "config_revision": int(self.config_revision or 0),
            "config_hash": self.config_hash or "",
            "lanes": self._lane_payload(for_controller=for_controller),
        }
        if not for_controller:
            payload["controller_codes"] = self._controller_codes_payload()
        return payload

    def prepare_sync_payload(self):
        """Payload for NSP Sync Odoo-to-Odoo.

        This must not require controller membership. Cloud can sync the master
        Gate/Lane/Antenna configuration to Edge before Edge controllers appear.
        """
        return self._gate_payload(for_controller=False, runtime=False)

    def prepare_controller_payload(self, for_controller=False):
        """Payload for runtime controller pull.

        This still validates Lane/Antenna readiness, but controller membership is
        handled by the API search domain before this method is called.
        """
        return self._gate_payload(for_controller=for_controller, runtime=True)


class NspGateLane(models.Model):
    _name = "nsp.gate.lane"
    _description = "NSP Gate Lane"
    _order = "gate_id, sequence, lane_no, id"
    _rec_name = "display_name"

    name = fields.Char(string="Lane Name", required=True, default="Lane")
    code = fields.Char(string="Lane Code", required=True, copy=False, index=True)
    display_name = fields.Char(string="Display Name", compute="_compute_display_name", store=True)
    gate_id = fields.Many2one("nsp.gate", string="Gate", required=True, ondelete="cascade", index=True)
    branch_id = fields.Many2one("nsp.branch", string="Branch", related="gate_id.branch_id", store=True, readonly=True)
    lane_no = fields.Integer(string="Lane No.", default=1, required=True)
    sequence = fields.Integer(string="Sequence", default=10)
    lane_type = fields.Selection([
        ("one_way", "One-way"),
        ("two_way", "Two-way"),
    ], string="Lane Type", required=True, default="one_way", index=True)
    direction = fields.Selection([
        ("entry", "Entry"),
        ("exit", "Exit"),
        ("both", "Legacy Both"),
    ], string="One-way Direction", required=True, default="entry", index=True,
       help="For one-way lanes only. Legacy value 'both' is migrated to lane_type=two_way.")
    detection_window_ms = fields.Integer(string="Detection Window (ms)", default=1500, required=True)
    required_vehicle_tid = fields.Boolean(string="Require Vehicle TID", default=True)
    required_user_tid = fields.Boolean(string="Require User TID", default=False)
    required_antenna_count = fields.Integer(string="Legacy Required Antennas", default=1)
    effective_required_antenna_count = fields.Integer(string="Effective Required Antennas", compute="_compute_effective_required_antenna_count")
    requires_user_tid = fields.Boolean(string="Requires User TID", compute="_compute_requires_user_tid", store=False)
    active = fields.Boolean(string="Active", default=True, index=True)
    antenna_group_ids = fields.One2many("nsp.gate.lane.antenna.group", "lane_id", string="Antenna Groups")
    antenna_rule_ids = fields.One2many("nsp.gate.lane.antenna.mapping", "lane_id", string="Antenna Mapping (Legacy Relation)")
    active_antenna_count = fields.Integer(string="Active Antennas", compute="_compute_active_antenna_count")
    note = fields.Text(string="Note")

    _sql_constraints = [
        ("unique_gate_lane_code", "unique(gate_id, code)", "Lane Code must be unique inside one Gate."),
        ("unique_gate_lane_no", "unique(gate_id, lane_no)", "Lane No. must be unique inside one Gate."),
        ("lane_no_positive", "CHECK(lane_no > 0)", "Lane No. must be positive."),
        ("required_antenna_count_positive", "CHECK(required_antenna_count >= 1)", "Required Antennas must be at least 1."),
    ]

    @api.depends("gate_id.code", "code", "name", "lane_type", "direction")
    def _compute_display_name(self):
        for lane in self:
            direction_label = "Two-way" if lane.lane_type == "two_way" else dict(lane._fields["direction"].selection).get(lane.direction, lane.direction or "")
            lane.display_name = "%s / %s / %s" % (lane.gate_id.code or "Gate", lane.code or lane.name or "Lane", direction_label)

    @api.depends("antenna_rule_ids.is_active", "required_antenna_count")
    def _compute_effective_required_antenna_count(self):
        for lane in self:
            active_rules = lane.antenna_rule_ids.filtered(lambda r: r.is_active and r.antenna_ref_id)
            lane.effective_required_antenna_count = max(1, min(int(lane.required_antenna_count or 1), len(active_rules) or int(lane.required_antenna_count or 1)))

    @api.depends("required_user_tid")
    def _compute_requires_user_tid(self):
        for lane in self:
            lane.requires_user_tid = bool(lane.required_user_tid)

    @api.depends("antenna_group_ids.antenna_mapping_ids.is_active")
    def _compute_active_antenna_count(self):
        for lane in self:
            lane.active_antenna_count = len(lane.antenna_group_ids.mapped("antenna_mapping_ids").filtered(lambda rec: rec.is_active))

    @api.model
    def _normalize_code(self, value):
        return str(value or "").strip().upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals["code"] = self._normalize_code(vals.get("code") or vals.get("name") or ("LANE-%s" % (vals.get("lane_no") or "")))
            if not vals.get("lane_type"):
                vals["lane_type"] = "two_way" if vals.get("direction") == "both" else "one_way"
            if vals.get("lane_type") == "two_way":
                vals["direction"] = "both"
            elif vals.get("direction") not in ("entry", "exit"):
                vals["direction"] = "entry"
        records = super().create(vals_list)
        records.mapped("gate_id")._refresh_config_hash(bump_if_changed=True)
        return records

    def write(self, vals):
        vals = dict(vals)
        if "code" in vals:
            vals["code"] = self._normalize_code(vals.get("code"))
        if vals.get("lane_type") == "two_way":
            vals["direction"] = "both"
        elif vals.get("lane_type") == "one_way" and vals.get("direction") not in ("entry", "exit"):
            vals["direction"] = "entry"
        gates = self.mapped("gate_id")
        res = super().write(vals)
        watched = {"name", "code", "gate_id", "lane_no", "sequence", "lane_type", "direction", "detection_window_ms", "required_vehicle_tid", "required_user_tid", "active"}
        if watched.intersection(vals.keys()):
            (gates | self.mapped("gate_id"))._refresh_config_hash(bump_if_changed=True)
        return res

    @api.constrains("lane_type", "direction", "detection_window_ms")
    def _check_lane_contract(self):
        for lane in self:
            if lane.lane_type == "one_way" and lane.direction not in ("entry", "exit"):
                raise ValidationError(_("One-way Lane direction must be entry or exit."))
            if lane.lane_type == "two_way" and lane.direction != "both":
                raise ValidationError(_("Two-way Lane uses entry/exit Antenna Groups; its legacy direction must remain both."))
            if int(lane.detection_window_ms or 0) <= 0:
                raise ValidationError(_("Detection Window must be greater than zero."))

    def unlink(self):
        gates = self.mapped("gate_id")
        res = super().unlink()
        gates._refresh_config_hash(bump_if_changed=True)
        return res


class NspGateLaneAntennaGroup(models.Model):
    _name = "nsp.gate.lane.antenna.group"
    _description = "NSP Lane Antenna Group"
    _order = "lane_id, sequence, direction, id"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=True)
    lane_id = fields.Many2one("nsp.gate.lane", required=True, ondelete="cascade", index=True)
    gate_id = fields.Many2one("nsp.gate", related="lane_id.gate_id", store=True, readonly=True, index=True)
    direction = fields.Selection([("entry", "Entry"), ("exit", "Exit")], required=True, index=True)
    detection_mode = fields.Selection([("sequential", "Sequential"), ("any", "Any")], required=True, default="sequential", index=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, index=True)
    antenna_mapping_ids = fields.One2many("nsp.gate.lane.antenna.mapping", "antenna_group_id", string="Antennas")

    _sql_constraints = [
        ("unique_lane_direction", "unique(lane_id, direction)", "A Lane can have only one Antenna Group per direction."),
    ]

    @api.depends("lane_id", "direction", "detection_mode")
    def _compute_display_name(self):
        for group in self:
            group.display_name = "%s / %s / %s" % (group.lane_id.display_name or group.lane_id.code or "Lane", group.direction or "", group.detection_mode or "")

    @api.constrains("lane_id", "direction")
    def _check_lane_direction(self):
        for group in self:
            if group.lane_id.lane_type == "one_way" and group.direction != group.lane_id.direction:
                raise ValidationError(_("A one-way Lane Antenna Group must match the Lane direction."))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.mapped("gate_id")._refresh_config_hash(bump_if_changed=True)
        return records

    def write(self, vals):
        gates = self.mapped("gate_id")
        result = super().write(vals)
        if {"lane_id", "direction", "detection_mode", "sequence", "active"}.intersection(vals):
            (gates | self.mapped("gate_id"))._refresh_config_hash(bump_if_changed=True)
        return result

    def unlink(self):
        gates = self.mapped("gate_id")
        result = super().unlink()
        gates._refresh_config_hash(bump_if_changed=True)
        return result


class NspGateLaneAntennaRule(models.Model):
    _name = "nsp.gate.lane.antenna.mapping"
    _description = "NSP Gate Lane Antenna Mapping"
    _order = "gate_id, lane_id, sequence_order, antenna_ref_id, id"
    _rec_name = "rule_name"

    rule_name = fields.Char(string="Rule", compute="_compute_rule_name", store=True)
    gate_id = fields.Many2one("nsp.gate", string="Gate", required=True, ondelete="cascade", index=True)
    antenna_group_id = fields.Many2one("nsp.gate.lane.antenna.group", string="Antenna Group", ondelete="cascade", index=True)
    lane_id = fields.Many2one("nsp.gate.lane", string="Lane", required=True, ondelete="cascade", index=True)
    lane_direction = fields.Selection(related="lane_id.direction", string="Legacy Lane Direction", store=True, readonly=True)
    group_direction = fields.Selection(related="antenna_group_id.direction", string="Direction", store=True, readonly=True, index=True)
    detection_mode = fields.Selection(related="antenna_group_id.detection_mode", string="Detection Mode", store=True, readonly=True, index=True)
    antenna_direction = fields.Selection([
        ("auto", "Auto / Follow Lane"),
        ("entry", "Entry"),
        ("exit", "Exit"),
        ("both", "Both"),
    ], string="Antenna Direction", default="auto", required=True, index=True)
    tag_role = fields.Selection([
        ("vehicle_tid", "Vehicle TID"),
        ("user_tid", "User TID"),
        ("both", "Vehicle/User TID"),
    ], string="RFID Role", default="vehicle_tid", required=True, index=True,
       help="Defines whether this antenna is expected to read the vehicle RFID card, the employee RFID card, or both.")
    effective_direction = fields.Selection([
        ("entry", "Entry"),
        ("exit", "Exit"),
        ("both", "Both"),
    ], string="Effective Direction", compute="_compute_effective_direction", store=True, readonly=True, index=True)
    antenna_ref_id = fields.Many2one("nsp.device.antenna", string="Physical Antenna", required=True, ondelete="cascade", index=True)
    controller_id = fields.Many2one("nsp.controller", string="Controller", related="antenna_ref_id.device_id.controller_id", store=True, readonly=True, index=True)
    device_id = fields.Many2one("nsp.device", string="Device", related="antenna_ref_id.device_id", store=True, readonly=True, index=True)
    serial_number = fields.Char(string="Serial Number", related="device_id.serial_number", store=True, readonly=True, index=True)
    antenna_no = fields.Integer(string="Antenna No", related="antenna_ref_id.antenna_id", store=True, readonly=True, index=True)
    device_serial = fields.Char(string="Legacy Device Serial", related="device_id.serial_number", store=True, readonly=True, index=True)
    antenna_id = fields.Integer(string="Legacy Antenna ID", related="antenna_ref_id.antenna_id", store=True, readonly=True, index=True)
    sequence_no = fields.Integer(string="Sequence No", default=0)
    sequence_order = fields.Integer(string="Legacy Sequence", default=0)
    required = fields.Boolean(string="Required", default=True)
    is_active = fields.Boolean(string="Active", default=True)
    note = fields.Char(string="Note")

    _sql_constraints = [
        ("unique_group_antenna", "unique(antenna_group_id, antenna_ref_id)", "This physical antenna is already mapped to this Antenna Group."),
    ]

    @api.depends("lane_id", "lane_id.direction", "antenna_direction")
    def _compute_effective_direction(self):
        for rule in self:
            if rule.antenna_group_id:
                rule.effective_direction = rule.antenna_group_id.direction
            elif rule.antenna_direction and rule.antenna_direction != "auto":
                rule.effective_direction = rule.antenna_direction
            else:
                rule.effective_direction = rule.lane_id.direction or "both"

    @api.depends("gate_id", "lane_id", "antenna_group_id", "antenna_ref_id", "serial_number", "antenna_no", "group_direction", "detection_mode", "sequence_no")
    def _compute_rule_name(self):
        for rule in self:
            rule.rule_name = "%s / %s / %s Ant %s / %s / %s / Seq %s" % (
                rule.gate_id.code or "Gate",
                rule.lane_id.code or "Lane",
                rule.serial_number or "Device",
                rule.antenna_no or "",
                rule.group_direction or rule.effective_direction or "",
                rule.detection_mode or "",
                rule.sequence_no or 0,
            )

    @api.onchange("antenna_group_id", "lane_id")
    def _onchange_lane_id(self):
        for rule in self:
            if rule.antenna_group_id:
                rule.lane_id = rule.antenna_group_id.lane_id
                rule.gate_id = rule.antenna_group_id.gate_id
            elif rule.lane_id:
                rule.gate_id = rule.lane_id.gate_id

    @api.constrains("lane_id", "gate_id", "antenna_ref_id")
    def _check_mapping_scope(self):
        for rule in self:
            if rule.antenna_group_id and rule.antenna_group_id.lane_id != rule.lane_id:
                raise ValidationError(_("Antenna Group must belong to the selected Lane."))
            if rule.lane_id.gate_id != rule.gate_id:
                raise ValidationError(_("Lane must belong to the same Gate."))
            if rule.antenna_ref_id and rule.controller_id and not rule.gate_id._has_controller(rule.controller_id):
                raise ValidationError(_("Physical antenna must belong to one of the Gate Controllers."))

    @api.constrains("antenna_group_id", "sequence_no")
    def _check_mapping_contract(self):
        for rule in self:
            if not rule.antenna_group_id:
                continue
            if rule.antenna_group_id.detection_mode == "sequential" and int(rule.sequence_no or 0) <= 0:
                raise ValidationError(_("Sequential antenna mapping requires sequence_no greater than zero."))
            if rule.antenna_group_id.detection_mode == "any" and int(rule.sequence_no or 0) != 0:
                raise ValidationError(_("Any-mode antenna mapping must not define sequence_no."))

    @api.model_create_multi
    def create(self, vals_list):
        Group = self.env["nsp.gate.lane.antenna.group"]
        for vals in vals_list:
            group = Group.browse(vals.get("antenna_group_id")).exists() if vals.get("antenna_group_id") else Group.browse()
            lane = group.lane_id if group else (self.env["nsp.gate.lane"].browse(vals.get("lane_id")).exists() if vals.get("lane_id") else self.env["nsp.gate.lane"].browse())
            if group:
                vals["lane_id"] = group.lane_id.id
                vals["gate_id"] = group.gate_id.id
            elif lane:
                vals.setdefault("gate_id", lane.gate_id.id)
            if vals.get("sequence_no") is None and vals.get("sequence_order") is not None:
                vals["sequence_no"] = vals.get("sequence_order") or 0
            vals["sequence_order"] = vals.get("sequence_no") or 0
        records = super().create(vals_list)
        records.mapped("gate_id")._refresh_config_hash(bump_if_changed=True)
        return records

    def write(self, vals):
        vals = dict(vals)
        if vals.get("antenna_group_id"):
            group = self.env["nsp.gate.lane.antenna.group"].browse(vals["antenna_group_id"]).exists()
            if group:
                vals["lane_id"] = group.lane_id.id
                vals["gate_id"] = group.gate_id.id
        elif vals.get("lane_id"):
            lane = self.env["nsp.gate.lane"].browse(vals.get("lane_id")).exists()
            if lane:
                vals.setdefault("gate_id", lane.gate_id.id)
        if "sequence_no" in vals:
            vals["sequence_order"] = vals.get("sequence_no") or 0
        gates = self.mapped("gate_id")
        res = super().write(vals)
        watched = {"antenna_group_id", "lane_id", "antenna_ref_id", "sequence_no", "is_active"}
        if watched.intersection(vals.keys()):
            (gates | self.mapped("gate_id"))._refresh_config_hash(bump_if_changed=True)
        return res

    def unlink(self):
        gates = self.mapped("gate_id")
        res = super().unlink()
        gates._refresh_config_hash(bump_if_changed=True)
        return res
