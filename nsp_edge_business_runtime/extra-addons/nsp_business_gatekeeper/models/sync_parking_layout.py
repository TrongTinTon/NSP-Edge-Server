# -*- coding: utf-8 -*-
from odoo import fields, models, _
from odoo.exceptions import UserError


class NspSyncJobParkingLayout(models.Model):
    _inherit = "nsp.sync.job"

    def _apply_parking_config(self, item):
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Parking Layout item must be an object."))
        unsupported = set(item) - {
            "parking_area_code", "parking_area_name", "branch_code", "state",
            "published_revision", "lanes",
        }
        if unsupported:
            raise UserError(
                _("Unsupported Parking Layout field(s): %s")
                % ", ".join(sorted(unsupported))
            )

        branch_code = self._normalize_sync_code(item.get("branch_code"))
        area_code = self._normalize_sync_code(item.get("parking_area_code"))
        if not branch_code or not area_code:
            raise UserError(_("Branch Code and Parking Area Code are required."))
        state = str(item.get("state") or "draft").strip().lower()
        if state not in ("draft", "operational", "maintenance", "blocked"):
            raise UserError(_("Invalid Parking Area state: %s") % state)
        try:
            published_revision = int(item.get("published_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Published Revision must be an integer.")) from exc
        if published_revision <= 0:
            raise UserError(_("Published Revision is required."))

        branch = self.env["nsp.branch"].sudo().with_context(active_test=False).search(
            [("code", "=", branch_code)], limit=1
        )
        if not branch:
            raise UserError(_("Branch %s was not found in the current snapshot.") % branch_code)

        Parking = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        parking = Parking.search([("code", "=", area_code)], limit=1)
        parking_values = {
            "code": area_code,
            "name": str(item.get("parking_area_name") or area_code).strip(),
            "branch_id": branch.id,
            "state": state,
            "published_revision": published_revision,
            "runtime_synced_at": fields.Datetime.now(),
        }
        if parking:
            Detection = self.env["nsp.parking.detection.event"].sudo()
            Detection._acquire_parking_area_runtime_lock(parking, shared=False)
            parking.invalidate_recordset(["state", "published_revision"])
            if int(parking.published_revision or 0) > published_revision:
                return parking
            Detection.invalidate_pending_for_runtime_change(
                parking, published_revision, state
            )
            self._write_changed(parking, parking_values)
        else:
            parking = Parking.create(parking_values)

        controllers = self.env["nsp.controller"].sudo().with_context(active_test=False).search([])
        controller_by_code = {
            self._normalize_sync_code(record.controller_id): record
            for record in controllers
        }
        readers = self.env["nsp.device"].sudo().with_context(active_test=False).search([])
        reader_by_code = {
            self._normalize_sync_code(record.device_code): record
            for record in readers if record.device_code
        }
        reader_by_id = {record.id: record for record in readers}
        declared_ports_by_reader = {
            self._normalize_sync_code(code): {int(port) for port in ports}
            for code, ports in (self.env.context.get("nsp_declared_reader_ports") or {}).items()
        }
        declared_config_by_reader = {
            self._normalize_sync_code(code): dict(values)
            for code, values in (self.env.context.get("nsp_declared_reader_configs") or {}).items()
        }

        lanes_data = item.get("lanes") or []
        if not isinstance(lanes_data, list):
            raise UserError(_("Parking Lanes must be an array."))

        lane_specs = {}
        timeline_specs = []
        sequence_specs = []
        reader_config_specs = []
        for lane_item in lanes_data:
            if not isinstance(lane_item, dict):
                raise UserError(_("Parking Lanes must contain objects."))
            unsupported_lane = set(lane_item) - {
                "lane_code", "lane_name", "server_code", "controller_code",
                "reader_port_timeline", "event_sequences", "timing_tolerance",
            }
            if unsupported_lane:
                raise UserError(
                    _("Unsupported Parking Lane field(s): %s")
                    % ", ".join(sorted(unsupported_lane))
                )

            lane_code = self._normalize_sync_code(lane_item.get("lane_code"))
            controller_code = self._normalize_sync_code(lane_item.get("controller_code"))
            server_code = self._normalize_sync_code(lane_item.get("server_code"))
            if not lane_code or lane_code in lane_specs or not controller_code:
                raise UserError(_("Parking Lane Code and Controller Code are required and must be unique."))
            controller = controller_by_code.get(controller_code)
            if not controller or not controller.active or controller.cloud_removed:
                raise UserError(_("Controller %s is missing or inactive.") % controller_code)
            if server_code and (
                not controller.edge_server_id
                or self._normalize_sync_code(controller.edge_server_id.edge_server_code) != server_code
            ):
                raise UserError(
                    _("Controller %(controller)s is not assembled under Server %(server)s.")
                    % {"controller": controller_code, "server": server_code}
                )

            tolerance = lane_item.get("timing_tolerance") or {}
            if not isinstance(tolerance, dict):
                raise UserError(_("Timing Tolerance must be an object."))
            unsupported_tolerance = set(tolerance) - {"type", "value"}
            if unsupported_tolerance:
                raise UserError(
                    _("Unsupported Timing Tolerance field(s): %s")
                    % ", ".join(sorted(unsupported_tolerance))
                )
            tolerance_type = str(tolerance.get("type") or "percent").strip().lower()
            if tolerance_type not in ("percent", "seconds"):
                raise UserError(_("Timing Tolerance type must be percent or seconds."))
            try:
                tolerance_value = float(tolerance.get("value") or 0.0)
            except (TypeError, ValueError) as exc:
                raise UserError(_("Timing Tolerance value must be numeric.")) from exc
            if tolerance_value < 0:
                raise UserError(_("Timing Tolerance cannot be negative."))

            lane_specs[lane_code] = {
                "parking_area_id": parking.id,
                "code": lane_code,
                "name": str(lane_item.get("lane_name") or lane_code).strip(),
                "controller_id": controller.id,
                "tolerance_type": tolerance_type,
                "tolerance_value": tolerance_value,
                "active": True,
            }

            timeline = lane_item.get("reader_port_timeline") or []
            if not isinstance(timeline, list):
                raise UserError(_("Reader Port Timeline must be an array."))
            if state == "operational" and len(timeline) < 2:
                raise UserError(
                    _("Operational Lane %s requires at least two Reader Port Timeline points.")
                    % lane_code
                )
            seen_orders = set()
            seen_refs = set()
            timeline_position = {}
            for row in timeline:
                if not isinstance(row, dict):
                    raise UserError(_("Reader Port Timeline rows must contain objects."))
                unsupported_timeline = set(row) - {
                    "sequence", "reader_code", "port_no",
                    "duration_from_previous_seconds", "cumulative_time_seconds",
                }
                if unsupported_timeline:
                    raise UserError(
                        _("Unsupported Reader Port Timeline field(s): %s")
                        % ", ".join(sorted(unsupported_timeline))
                    )
                reader_code = self._normalize_sync_code(row.get("reader_code"))
                try:
                    sequence = int(row.get("sequence") or 0)
                    port_no = int(row.get("port_no") or 0)
                    duration = float(row.get("duration_from_previous_seconds") or 0.0)
                    cumulative = float(row.get("cumulative_time_seconds") or 0.0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Reader Port Timeline value.")) from exc
                reader = reader_by_code.get(reader_code)
                ref = (reader.id, port_no) if reader else False
                if sequence <= 0 or port_no < 1 or port_no > 16 or not reader:
                    raise UserError(_("Reader Port Timeline references an invalid Reader or Port."))
                if reader.controller_id != controller or not reader.active or reader.cloud_removed:
                    raise UserError(_("Every Timeline Reader must be active and belong to the Lane Controller."))
                declared_ports = declared_ports_by_reader.get(reader_code)
                if declared_ports is not None and port_no not in declared_ports:
                    raise UserError(_("Timeline Port is not declared by the published Reader assembly."))
                if sequence in seen_orders or ref in seen_refs:
                    raise UserError(_("Timeline Order and Reader Port must be unique per Lane."))
                if sequence == 1 and duration != 0.0:
                    raise UserError(_("The first Timeline point must have zero Duration from previous."))
                if sequence > 1 and duration <= 0.0:
                    raise UserError(_("Every Timeline point after the first requires a positive Duration."))
                seen_orders.add(sequence)
                seen_refs.add(ref)
                timeline_position[ref] = sequence
                timeline_specs.append({
                    "lane_code": lane_code,
                    "sequence": sequence,
                    "reader_id": reader.id,
                    "port_no": port_no,
                    "duration_from_previous": duration,
                    "cumulative_time": max(0.0, cumulative),
                })
            if seen_orders and seen_orders != set(range(1, len(seen_orders) + 1)):
                raise UserError(_("Reader Port Timeline Order must be contiguous and start at 1."))

            for reader_id in sorted({reader_id for reader_id, _port in seen_refs}):
                reader = reader_by_id.get(reader_id)
                reader_code = self._normalize_sync_code(reader.device_code)
                config = declared_config_by_reader.get(reader_code)
                if not config:
                    raise UserError(
                        _("Published Reader Configuration is missing for %s.") % reader_code
                    )
                reader_config_specs.append({
                    "lane_code": lane_code,
                    "reader_id": reader.id,
                    "power_dbm": int(config["power_dbm"]),
                    "read_interval_ms": int(config["read_interval_ms"]),
                    "tid_start_address": int(config["tid_start_address"]),
                    "tid_length": int(config["tid_length"]),
                    "source_type": "published_layout",
                })

            event_sequences = lane_item.get("event_sequences") or {}
            if not isinstance(event_sequences, dict):
                raise UserError(_("Event Sequences must be an object."))
            unsupported_events = set(event_sequences) - {"check_in", "check_out"}
            if unsupported_events:
                raise UserError(
                    _("Unsupported Event Sequence type(s): %s")
                    % ", ".join(sorted(unsupported_events))
                )
            configured_count = 0
            orientation_by_type = {}
            for sequence_type in ("check_in", "check_out"):
                values = event_sequences.get(sequence_type) or []
                if not isinstance(values, list):
                    raise UserError(_("Each Event Sequence must be an array."))
                if values and len(values) < 2:
                    raise UserError(_("Each configured Event Sequence requires at least two Reader Ports."))
                seen_sequence_refs = set()
                positions = []
                for order, step in enumerate(values, start=1):
                    if not isinstance(step, dict) or set(step) - {"reader_code", "port_no"}:
                        raise UserError(_("Event Sequence steps must contain Reader Code and Port No."))
                    reader_code = self._normalize_sync_code(step.get("reader_code"))
                    try:
                        port_no = int(step.get("port_no") or 0)
                    except (TypeError, ValueError) as exc:
                        raise UserError(_("Event Sequence Port No. must be an integer.")) from exc
                    reader = reader_by_code.get(reader_code)
                    ref = (reader.id, port_no) if reader else False
                    if not reader or ref not in seen_refs:
                        raise UserError(_("Event Sequence Reader Port must exist in the Lane Timeline."))
                    if ref in seen_sequence_refs:
                        raise UserError(_("A Reader Port can appear only once in one Event Sequence."))
                    seen_sequence_refs.add(ref)
                    positions.append(timeline_position[ref])
                    sequence_specs.append({
                        "lane_code": lane_code,
                        "sequence_type": sequence_type,
                        "sequence": order,
                        "reader_id": reader.id,
                        "port_no": port_no,
                    })
                if any(abs(current - previous) != 1 for previous, current in zip(positions, positions[1:])):
                    raise UserError(_("Event Sequence must follow adjacent points in the Reader Port Timeline."))
                if len(positions) >= 2:
                    orientation_by_type[sequence_type] = 1 if positions[1] > positions[0] else -1
                configured_count += bool(values)
            if (
                orientation_by_type.get("check_in")
                and orientation_by_type.get("check_out")
                and orientation_by_type["check_in"] == orientation_by_type["check_out"]
            ):
                raise UserError(_("Check-in and Check-out Sequences must follow opposite Timeline directions."))
            if state == "operational" and not configured_count:
                raise UserError(
                    _("Operational Lane %s requires at least one Check-in or Check-out Sequence.")
                    % lane_code
                )

        Lane = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        existing_lanes = Lane.search([("parking_area_id", "=", parking.id)])
        lane_by_code = {lane.code: lane for lane in existing_lanes}
        for lane_code, values in lane_specs.items():
            lane = lane_by_code.get(lane_code)
            if lane:
                self._write_changed(lane, values)
            else:
                lane = Lane.create(values)
                lane_by_code[lane_code] = lane

        area_lanes = Lane.search([("parking_area_id", "=", parking.id)])
        Timeline = self.env["nsp.parking.lane.timeline"].sudo()
        Sequence = self.env["nsp.parking.lane.event.sequence"].sudo()
        ReaderConfig = self.env["nsp.parking.lane.reader.config"].sudo()
        existing_sequences = Sequence.search([("lane_id", "in", area_lanes.ids)]) if area_lanes else Sequence.browse()
        existing_timeline = Timeline.search([("lane_id", "in", area_lanes.ids)]) if area_lanes else Timeline.browse()
        existing_reader_configs = ReaderConfig.search([("lane_id", "in", area_lanes.ids)]) if area_lanes else ReaderConfig.browse()
        if existing_sequences:
            existing_sequences.unlink()
        if existing_timeline:
            existing_timeline.unlink()
        if existing_reader_configs:
            existing_reader_configs.unlink()

        timeline_values = []
        for spec in timeline_specs:
            values = dict(spec)
            values["lane_id"] = lane_by_code[values.pop("lane_code")].id
            timeline_values.append(values)
        if timeline_values:
            Timeline.create(timeline_values)

        sequence_values = []
        for spec in sequence_specs:
            values = dict(spec)
            values["lane_id"] = lane_by_code[values.pop("lane_code")].id
            sequence_values.append(values)
        if sequence_values:
            Sequence.create(sequence_values)

        reader_config_values = []
        for spec in reader_config_specs:
            values = dict(spec)
            values["lane_id"] = lane_by_code[values.pop("lane_code")].id
            reader_config_values.append(values)
        if reader_config_values:
            ReaderConfig.create(reader_config_values)

        incoming_lane_codes = set(lane_specs)
        stale_lanes = area_lanes.filtered(
            lambda lane: lane.code not in incoming_lane_codes and lane.active
        )
        if stale_lanes:
            stale_lanes.mapped("timeline_line_ids").unlink()
            stale_lanes.mapped("event_sequence_ids").unlink()
            stale_lanes.mapped("reader_config_ids").unlink()
            stale_lanes.write({"active": False})

        if parking.state == "operational":
            issues = parking._operational_issues()
            if issues:
                raise UserError("; ".join(str(issue) for issue in issues))
        return parking

    def _validate_operational_parking_topology(self):
        self.ensure_one()
        rows = self.env["nsp.parking.lane.timeline"].sudo().search([
            ("lane_id.active", "=", True),
            ("lane_id.parking_area_id.state", "=", "operational"),
        ])
        lane_by_ref = {}
        conflicts = []
        for row in rows:
            ref = (row.reader_id.id, int(row.port_no or 0))
            previous = lane_by_ref.get(ref)
            if previous and previous != row.lane_id:
                conflicts.append(
                    "%s / Port %s: %s / %s" % (
                        row.reader_id.display_name,
                        row.port_no,
                        previous.display_name,
                        row.lane_id.display_name,
                    )
                )
            else:
                lane_by_ref[ref] = row.lane_id
        if conflicts:
            raise UserError(
                _("Operational Parking topology contains duplicated Reader Port assignments: %s")
                % "; ".join(sorted(set(conflicts)))
            )
        return True

    def _reconcile_parking_config_snapshot(self, items):
        self.ensure_one()
        incoming_codes = {
            self._normalize_sync_code(item.get("parking_area_code"))
            for item in (items or [])
            if isinstance(item, dict) and item.get("parking_area_code")
        }
        Parking = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        stale = Parking.search([("code", "not in", list(incoming_codes))]) if incoming_codes else Parking.search([])
        if stale:
            stale.mapped("lane_ids.timeline_line_ids").unlink()
            stale.mapped("lane_ids.event_sequence_ids").unlink()
            stale.mapped("lane_ids.reader_config_ids").unlink()
            stale.mapped("lane_ids").write({"active": False})
            stale._apply_parking_state_transition("blocked", force=True)
        return len(stale)

    @staticmethod
    def _normalize_sync_code(value):
        return str(value or "").strip().upper()

    def _apply_parking_runtime_snapshot(self, data, request_payload=False):
        self.ensure_one()
        if not isinstance(data, dict):
            raise UserError(_("Parking Runtime response must be an object."))
        try:
            revision = int(data.get("revision") or 0)
        except (TypeError, ValueError) as exc:
            raise UserError(_("Parking Runtime revision must be an integer.")) from exc
        if revision <= 0:
            raise UserError(_("Parking Runtime revision is required."))
        if revision < int(self.snapshot_revision or 0):
            return {"applied": 0, "removed": 0, "revision": revision, "stale": True}

        controllers = data.get("controllers") or []
        areas = data.get("parking_areas") or []
        branches = data.get("branches") or []
        whitelist = data.get("device_whitelist") or []
        for name, value in (
            ("controllers", controllers),
            ("parking_areas", areas),
            ("branches", branches),
            ("device_whitelist", whitelist),
        ):
            if not isinstance(value, list):
                raise UserError(_("Parking Runtime %s must be an array.") % name)

        with self.env.cr.savepoint():
            Branch = self.env["nsp.branch"].sudo().with_context(active_test=False)
            existing_branches = {record.code: record for record in Branch.search([])}
            incoming_branch_codes = set()
            for item in branches:
                if not isinstance(item, dict) or set(item) - {
                    "branch_code", "branch_name", "timezone", "active",
                }:
                    raise UserError(_("Invalid Branch payload."))
                code = str(item.get("branch_code") or "").strip().upper()
                if not code:
                    raise UserError(_("Branch Code is required."))
                incoming_branch_codes.add(code)
                values = {
                    "name": item.get("branch_name") or code,
                    "code": code,
                    "timezone": item.get("timezone") or "Asia/Ho_Chi_Minh",
                    "status": "active" if item.get("active", True) else "inactive",
                }
                record = existing_branches.get(code)
                if record:
                    self._write_changed(record, values)
                else:
                    existing_branches[code] = Branch.create(values)
            stale_branches = Branch.search([
                ("code", "not in", list(incoming_branch_codes)),
            ]) if incoming_branch_codes else Branch.search([])
            if stale_branches:
                stale_branches.write({"status": "inactive"})

            identity_cache = self._prepare_apply_cache("device_whitelist", whitelist)
            for item in whitelist:
                self._apply_device_whitelist(item, cache=identity_cache)
            Whitelist = self.env["nsp.device.whitelist"].sudo().with_context(active_test=False)
            whitelist_by_code = {
                record.technical_code: record
                for record in Whitelist.search([])
                if record.technical_code
            }

            Edge = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
            edge_by_code = {record.edge_server_code: record for record in Edge.search([])}
            for identity in whitelist:
                if str(identity.get("device_type_code") or "").strip().upper() != "SERVER":
                    continue
                code = str(identity.get("technical_code") or "").strip().upper()
                whitelist_record = whitelist_by_code.get(code)
                if not code or not whitelist_record:
                    raise UserError(_("Published Server identity is invalid."))
                if whitelist_record.device_type_code != "SERVER":
                    raise UserError(_("Published Server identity has an invalid Device Type."))
                values = {
                    "name": str(identity.get("name") or code).strip(),
                    "whitelist_id": whitelist_record.id,
                    "active": bool(identity.get("active", True)),
                    "cloud_removed": False,
                }
                edge = edge_by_code.get(code)
                if edge:
                    self._write_changed(edge, values)
                else:
                    edge = Edge.create({"edge_server_code": code, **values})
                    edge_by_code[code] = edge

            Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
            Device = self.env["nsp.device"].sudo().with_context(active_test=False)
            controller_by_code = {record.controller_id: record for record in Controller.search([])}
            reader_by_code = {record.device_code: record for record in Device.search([])}
            reader_by_serial = {record.serial_number: record for record in Device.search([])}
            seen_controller_codes = set()
            reader_owner = {}
            declared_ports_by_reader = {}
            declared_config_by_reader = {}

            for controller_item in controllers:
                if not isinstance(controller_item, dict):
                    raise UserError(_("Controller payload must contain objects."))
                unsupported = set(controller_item) - {
                    "controller_code", "controller_name", "server_code", "active", "devices",
                }
                if unsupported:
                    raise UserError(
                        _("Unsupported Controller field(s): %s")
                        % ", ".join(sorted(unsupported))
                    )
                controller_code = str(controller_item.get("controller_code") or "").strip().upper()
                server_code = str(controller_item.get("server_code") or "").strip().upper()
                if not controller_code or not server_code:
                    raise UserError(_("Controller Code and Server Code are required."))
                if controller_code in seen_controller_codes:
                    raise UserError(_("Controller %s is duplicated in Parking Runtime.") % controller_code)
                seen_controller_codes.add(controller_code)
                edge = edge_by_code.get(server_code)
                controller_whitelist = whitelist_by_code.get(controller_code)
                if not edge or not controller_whitelist:
                    raise UserError(_("Published Controller assembly is missing from the identity projection."))
                if controller_whitelist.device_type_code != "CONTROLLER":
                    raise UserError(_("Published Controller identity has an invalid Device Type."))
                controller_values = {
                    "controller_name": controller_item.get("controller_name") or controller_code,
                    "edge_server_id": edge.id,
                    "active": bool(controller_item.get("active", True)),
                    "cloud_removed": False,
                    "whitelist_id": controller_whitelist.id,
                }
                controller = controller_by_code.get(controller_code)
                if controller:
                    self._write_changed(controller, controller_values)
                else:
                    controller = Controller.create({"controller_id": controller_code, **controller_values})
                    controller_by_code[controller_code] = controller

                devices = controller_item.get("devices") or []
                if not isinstance(devices, list):
                    raise UserError(_("Controller devices must be an array."))
                for reader_item in devices:
                    if not isinstance(reader_item, dict):
                        raise UserError(_("RFID Reader payload must contain objects."))
                    unsupported_reader = set(reader_item) - {
                        "technical_code", "serial_number", "reader_name",
                        "physical_connection", "reader_parameters", "ports",
                    }
                    if unsupported_reader:
                        raise UserError(
                            _("Unsupported RFID Reader field(s): %s")
                            % ", ".join(sorted(unsupported_reader))
                        )
                    serial = str(reader_item.get("serial_number") or "").strip().upper()
                    reader_code = str(reader_item.get("technical_code") or "").strip().upper()
                    if not serial or not reader_code:
                        raise UserError(_("RFID Reader Code and Serial Number are required."))
                    previous_owner = reader_owner.get(reader_code)
                    if previous_owner and previous_owner != controller_code:
                        raise UserError(_("RFID Reader %s is published under multiple Controllers.") % reader_code)
                    reader_owner[reader_code] = controller_code
                    existing_by_code = reader_by_code.get(reader_code)
                    existing_by_serial = reader_by_serial.get(serial)
                    if (
                        existing_by_code
                        and existing_by_serial
                        and existing_by_code != existing_by_serial
                    ):
                        raise UserError(_(
                            "RFID Reader identity is duplicated on Edge: "
                            "Reader Code %(code)s and Serial %(serial)s belong to different records."
                        ) % {
                            "code": reader_code,
                            "serial": serial,
                        })
                    reader_whitelist = whitelist_by_code.get(reader_code)
                    if not reader_whitelist:
                        raise UserError(_("Published RFID Reader %s is missing from the identity projection.") % reader_code)
                    if reader_whitelist.device_type_code != "RFID_READER":
                        raise UserError(_("Published RFID Reader identity has an invalid Device Type."))
                    parameters = reader_item.get("reader_parameters") or {}
                    if not isinstance(parameters, dict) or set(parameters) - {
                        "power_dbm", "read_interval_ms", "tid_start_address", "tid_length",
                    }:
                        raise UserError(_("Invalid Reader Parameters payload."))
                    try:
                        power = int(parameters.get("power_dbm") if parameters.get("power_dbm") is not None else 30)
                        interval = int(parameters.get("read_interval_ms") or 200)
                        tid_addr = int(parameters.get("tid_start_address") or 0)
                        tid_len = int(parameters.get("tid_length") or 4)
                    except (TypeError, ValueError) as exc:
                        raise UserError(_("Invalid Reader Parameters.")) from exc
                    if power < 0 or power > 40 or interval <= 0 or interval > 60000 or tid_addr < 0 or tid_len <= 0:
                        raise UserError(_("Reader Parameters are outside the supported range."))

                    ports = reader_item.get("ports") or []
                    if not isinstance(ports, list) or not ports:
                        raise UserError(_("Published RFID Reader %s has no Reader Ports.") % serial)
                    port_numbers = set()
                    for port in ports:
                        if not isinstance(port, dict) or set(port) - {"port_no"}:
                            raise UserError(_("Reader Port payload contains unsupported fields."))
                        try:
                            port_no = int(port.get("port_no") or 0)
                        except (TypeError, ValueError) as exc:
                            raise UserError(_("Reader Port No. must be an integer.")) from exc
                        if port_no < 1 or port_no > 16 or port_no in port_numbers:
                            raise UserError(_("Reader Port must be unique and between 1 and 16."))
                        port_numbers.add(port_no)
                    previous_ports = declared_ports_by_reader.get(reader_code)
                    if previous_ports is not None and previous_ports != port_numbers:
                        raise UserError(_("RFID Reader %s has conflicting Reader Port declarations.") % reader_code)
                    declared_ports_by_reader[reader_code] = set(port_numbers)
                    current_config = {
                        "power_dbm": power,
                        "read_interval_ms": interval,
                        "tid_start_address": tid_addr,
                        "tid_length": tid_len,
                    }
                    previous_config = declared_config_by_reader.get(reader_code)
                    if previous_config and previous_config != current_config:
                        raise UserError(
                            _("RFID Reader %s has conflicting published parameters.") % reader_code
                        )
                    declared_config_by_reader[reader_code] = current_config

                    reader_values = {
                        "name": reader_item.get("reader_name") or serial,
                        "serial_number": serial,
                        "controller_id": controller.id,
                        "connection_type": reader_item.get("physical_connection") or False,
                        "power_dbm": power,
                        "read_interval_ms": interval,
                        "tid_addr": tid_addr,
                        "tid_len": tid_len,
                        "device_code": reader_code,
                        "whitelist_id": reader_whitelist.id,
                        "active": True,
                        "cloud_removed": False,
                    }
                    reader = existing_by_code or existing_by_serial
                    if reader:
                        previous_code = reader.device_code
                        previous_serial = reader.serial_number
                        self._write_changed(reader, reader_values)
                        if previous_code and previous_code != reader_code:
                            if reader_by_code.get(previous_code) == reader:
                                reader_by_code.pop(previous_code, None)
                        if previous_serial and previous_serial != serial:
                            if reader_by_serial.get(previous_serial) == reader:
                                reader_by_serial.pop(previous_serial, None)
                    else:
                        reader = Device.create(reader_values)
                    reader_by_code[reader_code] = reader
                    reader_by_serial[serial] = reader

            parking_sync = self.with_context(
                nsp_declared_reader_ports={
                    code: sorted(ports)
                    for code, ports in declared_ports_by_reader.items()
                },
                nsp_declared_reader_configs=declared_config_by_reader,
            )
            for area in areas:
                parking_sync._apply_parking_config(area)
            removed_area = self._reconcile_parking_config_snapshot(areas)
            self._validate_operational_parking_topology()
            self.write({
                "snapshot_revision": revision,
                "last_pull_at": fields.Datetime.now(),
                "sync_cursor": False,
            })

        return {
            "applied": len(controllers) + len(areas) + len(whitelist),
            "removed": removed_area,
            "revision": revision,
            "stale": False,
        }
