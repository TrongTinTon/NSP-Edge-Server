# -*- coding: utf-8 -*-
from odoo import fields, models, _
from odoo.exceptions import UserError


class NspSyncJobParkingLayout(models.Model):
    _inherit = "nsp.sync.job"

    def _apply_parking_config(self, item, snapshot_revision=0):
        """Apply one published Parking Layout into Lane Master + contextual Lane Configuration."""
        self.ensure_one()
        if not isinstance(item, dict):
            raise UserError(_("Parking Layout item must be an object."))
        unsupported = set(item) - {
            "parking_area_code", "parking_area_name", "branch_code", "state",
            "published_revision", "lanes",
        }
        if unsupported:
            raise UserError(_("Unsupported Parking Layout field(s): %s") % ", ".join(sorted(unsupported)))

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
            "runtime_snapshot_revision": int(snapshot_revision or 0),
            "runtime_synced_at": fields.Datetime.now(),
        }
        if parking:
            Detection = self.env["nsp.parking.detection.event"].sudo()
            Detection._acquire_parking_area_runtime_lock(parking, shared=False)
            parking.invalidate_recordset(["state", "published_revision"])
            if int(parking.published_revision or 0) > published_revision:
                return parking
            Detection.invalidate_pending_for_runtime_change(parking, published_revision, state)
            self._write_changed(parking, parking_values)
        else:
            parking = Parking.create(parking_values)

        edges = self.env["nsp.edge.server"].sudo().with_context(active_test=False).search([])
        edge_by_code = {
            self._normalize_sync_code(record.edge_server_code): record for record in edges
            if record.edge_server_code
        }
        controllers = self.env["nsp.controller"].sudo().with_context(active_test=False).search([])
        controller_by_code = {
            self._normalize_sync_code(record.controller_id): record for record in controllers
        }
        readers = self.env["nsp.device"].sudo().with_context(active_test=False).search([])
        reader_by_code = {
            self._normalize_sync_code(record.device_code): record
            for record in readers if record.device_code
        }
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

        lane_master_specs = {}
        layout_specs = {}
        sequence_specs = []
        reader_config_specs = []
        for lane_item in lanes_data:
            if not isinstance(lane_item, dict):
                raise UserError(_("Parking Lanes must contain objects."))
            unsupported_lane = set(lane_item) - {
                "lane_code", "lane_name", "server_code", "controller_code",
                "antenna_sequence", "readers",
            }
            if unsupported_lane:
                raise UserError(_("Unsupported Parking Lane field(s): %s") % ", ".join(sorted(unsupported_lane)))

            lane_code = self._normalize_sync_code(lane_item.get("lane_code"))
            controller_code = self._normalize_sync_code(lane_item.get("controller_code"))
            server_code = self._normalize_sync_code(lane_item.get("server_code"))
            if not lane_code or lane_code in layout_specs or not controller_code or not server_code:
                raise UserError(_(
                    "Parking Lane Code, Server Code and Controller Code are required; Lane Code must be unique."
                ))
            controller = controller_by_code.get(controller_code)
            edge = edge_by_code.get(server_code)
            if not controller or not controller.active or controller.cloud_removed:
                raise UserError(_("Controller %s is missing or inactive.") % controller_code)
            if not edge or not edge.active or edge.cloud_removed:
                raise UserError(_("Server %s is missing or inactive.") % server_code)

            lane_readers = lane_item.get("readers") or []
            if not isinstance(lane_readers, list) or not lane_readers:
                raise UserError(_("Parking Lane %s must contain Device Configuration readers.") % lane_code)
            lane_reader_by_code = {}
            for reader_item in lane_readers:
                if not isinstance(reader_item, dict):
                    raise UserError(_("Lane Reader Configuration must contain objects."))
                unsupported_reader = set(reader_item) - {
                    "technical_code", "serial_number", "reader_name", "reader_parameters", "ports",
                }
                if unsupported_reader:
                    raise UserError(_("Unsupported Lane Reader field(s): %s") % ", ".join(sorted(unsupported_reader)))
                reader_code = self._normalize_sync_code(reader_item.get("technical_code"))
                serial = str(reader_item.get("serial_number") or "").strip().upper()
                if not reader_code or reader_code in lane_reader_by_code:
                    raise UserError(_("Lane Reader technical_code is required and must be unique."))
                reader = reader_by_code.get(reader_code)
                if not reader or not reader.active or reader.cloud_removed:
                    raise UserError(_("Published RFID Reader %s is missing or inactive on Edge.") % reader_code)
                if serial and str(reader.serial_number or "").strip().upper() != serial:
                    raise UserError(_("Published RFID Reader serial does not match Edge identity: %s") % reader_code)

                parameters = reader_item.get("reader_parameters") or {}
                if not isinstance(parameters, dict) or set(parameters) - {
                    "power_dbm", "read_interval_ms", "tid_start_address", "tid_length",
                }:
                    raise UserError(_("Invalid Reader Parameters payload."))
                try:
                    config = {
                        "power_dbm": int(parameters.get("power_dbm") if parameters.get("power_dbm") is not None else 30),
                        "read_interval_ms": int(parameters.get("read_interval_ms") or 200),
                        "tid_start_address": int(parameters.get("tid_start_address") or 0),
                        "tid_length": int(parameters.get("tid_length") or 4),
                    }
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Reader Parameters.")) from exc
                if (
                    config["power_dbm"] < 0 or config["power_dbm"] > 40
                    or config["read_interval_ms"] <= 0 or config["read_interval_ms"] > 60000
                    or config["tid_start_address"] < 0 or config["tid_length"] <= 0
                ):
                    raise UserError(_("Reader Parameters are outside the supported range."))

                ports = reader_item.get("ports") or []
                if not isinstance(ports, list) or not ports:
                    raise UserError(_("Published RFID Reader %s has no Reader Ports.") % reader_code)
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

                declared_ports = declared_ports_by_reader.get(reader_code)
                if declared_ports is not None and not port_numbers.issubset(declared_ports):
                    raise UserError(_("Lane Reader Ports are outside the runtime Reader assembly: %s") % reader_code)
                declared_config = declared_config_by_reader.get(reader_code)
                if declared_config is not None and declared_config != config:
                    raise UserError(_("Lane Reader parameters conflict with the published Reader assembly: %s") % reader_code)

                lane_reader_by_code[reader_code] = {
                    "reader": reader, "ports": port_numbers, "config": config,
                }
                reader_config_specs.append({
                    "lane_code": lane_code,
                    "reader_id": reader.id,
                    "ports": sorted(port_numbers),
                    **config,
                })

            sequence = lane_item.get("antenna_sequence") or []
            if not isinstance(sequence, list):
                raise UserError(_("Antenna Sequence must be an array."))
            if state == "operational" and len(sequence) < 2:
                raise UserError(_("Operational Lane %s requires at least two Antenna Sequence points.") % lane_code)
            seen_refs = set()
            for expected_order, row in enumerate(sequence, start=1):
                if not isinstance(row, dict):
                    raise UserError(_("Antenna Sequence rows must contain objects."))
                unsupported_sequence = set(row) - {
                    "sequence", "reader_code", "reader_serial_number", "port_no",
                    "duration_from_previous_seconds",
                }
                if unsupported_sequence:
                    raise UserError(_("Unsupported Antenna Sequence field(s): %s") % ", ".join(sorted(unsupported_sequence)))
                reader_code = self._normalize_sync_code(row.get("reader_code"))
                try:
                    order = int(row.get("sequence") or 0)
                    port_no = int(row.get("port_no") or 0)
                    duration = float(row.get("duration_from_previous_seconds") or 0.0)
                except (TypeError, ValueError) as exc:
                    raise UserError(_("Invalid Antenna Sequence value.")) from exc
                if order != expected_order:
                    raise UserError(_("Antenna Sequence Order must be contiguous and start at 1."))
                reader_entry = lane_reader_by_code.get(reader_code)
                if not reader_entry or port_no not in reader_entry["ports"]:
                    raise UserError(_("Antenna Sequence can use only Ports declared in Lane Device Configuration."))
                reader = reader_entry["reader"]
                serial = str(row.get("reader_serial_number") or "").strip().upper()
                if serial and str(reader.serial_number or "").strip().upper() != serial:
                    raise UserError(_("Antenna Sequence Reader serial does not match Device Configuration."))
                ref = (reader.id, port_no)
                if ref in seen_refs:
                    raise UserError(_("A Reader Port can appear only once in an Antenna Sequence."))
                if expected_order == 1 and duration != 0.0:
                    raise UserError(_("The first Antenna Sequence point must use 0 seconds Max Duration."))
                if expected_order > 1 and duration <= 0.0:
                    raise UserError(_("Every Antenna after the first requires a positive Max Duration."))
                seen_refs.add(ref)
                sequence_specs.append({
                    "lane_code": lane_code,
                    "sequence": expected_order,
                    "reader_id": reader.id,
                    "port_no": port_no,
                    "duration_from_previous": 0.0 if expected_order == 1 else duration,
                })

            lane_master_specs[lane_code] = {
                "code": lane_code,
                "name": str(lane_item.get("lane_name") or lane_code).strip(),
                "branch_id": branch.id,
                "active": True,
            }
            layout_specs[lane_code] = {
                "parking_area_id": parking.id,
                "edge_server_id": edge.id,
                "controller_id": controller.id,
                "active": True,
            }

        LaneMaster = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        existing_masters = LaneMaster.search([("code", "in", list(lane_master_specs))]) if lane_master_specs else LaneMaster.browse()
        lane_master_by_code = {self._normalize_sync_code(row.code): row for row in existing_masters}
        for lane_code, values in lane_master_specs.items():
            lane = lane_master_by_code.get(lane_code)
            if lane:
                if lane.branch_id != branch:
                    raise UserError(_("Lane Master %s belongs to another Branch.") % lane_code)
                self._write_changed(lane, values)
            else:
                lane = LaneMaster.create(values)
                lane_master_by_code[lane_code] = lane

        LayoutLane = self.env["nsp.parking.layout.lane"].sudo().with_context(active_test=False)
        existing_layouts = LayoutLane.search([("parking_area_id", "=", parking.id)])
        layout_by_code = {
            self._normalize_sync_code(row.lane_id.code): row for row in existing_layouts
        }
        for lane_code, values in layout_specs.items():
            values = dict(values, lane_id=lane_master_by_code[lane_code].id)
            layout = layout_by_code.get(lane_code)
            if layout:
                self._write_changed(layout, values)
            else:
                layout = LayoutLane.create(values)
                layout_by_code[lane_code] = layout

        area_layouts = LayoutLane.search([("parking_area_id", "=", parking.id)])
        Sequence = self.env["nsp.parking.layout.lane.sequence"].sudo()
        ReaderConfig = self.env["nsp.parking.layout.lane.reader.config"].sudo()
        ReaderPort = self.env["nsp.parking.layout.lane.reader.port"].sudo()
        if area_layouts:
            Sequence.search([("layout_lane_id", "in", area_layouts.ids)]).unlink()
            ReaderConfig.search([("layout_lane_id", "in", area_layouts.ids)]).unlink()

        # Device Configuration owns Reader membership and enabled Ports. Persist it
        # completely before Antenna Sequence because Sequence only references the
        # already-configured Reader/Port pairs.
        config_values = []
        config_ports = []
        for spec in reader_config_specs:
            values = dict(spec)
            ports = values.pop("ports")
            lane_code = values.pop("lane_code")
            values["layout_lane_id"] = layout_by_code[lane_code].id
            config_values.append((lane_code, ports, values))
        created_configs = ReaderConfig.browse()
        if config_values:
            created_configs = ReaderConfig.create([values for _lane, _ports, values in config_values])
            for created, (_lane_code, ports, _values) in zip(created_configs, config_values):
                config_ports.extend({"reader_config_id": created.id, "port_no": port} for port in ports)
        if config_ports:
            ReaderPort.create(config_ports)

        sequence_values = []
        for spec in sequence_specs:
            values = dict(spec)
            values["layout_lane_id"] = layout_by_code[values.pop("lane_code")].id
            sequence_values.append(values)
        if sequence_values:
            Sequence.create(sequence_values)

        incoming_codes = set(layout_specs)
        stale_layouts = area_layouts.filtered(
            lambda row: self._normalize_sync_code(row.lane_id.code) not in incoming_codes and row.active
        )
        if stale_layouts:
            stale_layouts.mapped("antenna_sequence_ids").unlink()
            stale_layouts.mapped("reader_config_ids").unlink()
            stale_layouts.write({"active": False})

        if parking.state == "operational":
            issues = parking._operational_issues()
            if issues:
                raise UserError("; ".join(str(issue) for issue in issues))
        return parking

    def _validate_operational_parking_topology(self):
        self.ensure_one()
        reader_configs = self.env["nsp.parking.layout.lane.reader.config"].sudo().search([
            ("layout_lane_id.active", "=", True),
            ("layout_lane_id.parking_area_id.state", "=", "operational"),
        ])
        # Reader Port owns only reader_config_id + port_no. Lane and Reader are
        # already owned by Reader Configuration and must not be duplicated on each
        # port row. The same physical point may be reused by logical Lanes inside
        # one Parking Layout, but never by two operational Parking Layouts.
        areas_by_ref = {}
        conflicts = []
        for config in reader_configs:
            area_id = config.layout_lane_id.parking_area_id.id
            reader = config.reader_id
            for port in config.port_ids:
                ref = (reader.id, int(port.port_no or 0))
                area_ids = areas_by_ref.setdefault(ref, set())
                area_ids.add(area_id)
                if len(area_ids) > 1:
                    conflicts.append("%s / Port %s" % (reader.display_name, port.port_no))
        if conflicts:
            raise UserError(_(
                "Operational Parking Layouts cannot share Reader/Antenna points: %s"
            ) % "; ".join(sorted(set(conflicts))))

        # Physical identities stay independent, but the active runtime context must
        # still be unambiguous: one Controller executes under one Server and one
        # Reader is acquired by one Controller at a time. Reusing either identity
        # across multiple logical Lanes under the SAME context is valid.
        active_configs = self.env["nsp.parking.layout.lane"].sudo().search([
            ("active", "=", True),
            ("parking_area_id.state", "=", "operational"),
        ])
        servers_by_controller = {}
        controllers_by_reader = {}
        for configuration in active_configs:
            controller_id = configuration.controller_id.id
            server_ids = servers_by_controller.setdefault(controller_id, set())
            server_ids.add(configuration.edge_server_id.id)
            if len(server_ids) > 1:
                raise UserError(_(
                    "Controller %(controller)s is referenced under multiple Servers in active Parking Runtime."
                ) % {"controller": configuration.controller_id.display_name})
            for reader in configuration.reader_config_ids.mapped("reader_id"):
                controller_ids = controllers_by_reader.setdefault(reader.id, set())
                controller_ids.add(controller_id)
                if len(controller_ids) > 1:
                    raise UserError(_(
                        "Reader %(reader)s is referenced by multiple Controllers in active Parking Runtime."
                    ) % {"reader": reader.display_name})

        operational_areas = self.env["nsp.parking.area"].sudo().search([("state", "=", "operational")])
        for parking_area in operational_areas:
            configurations = parking_area.layout_lane_ids.filtered("active")
            lane_sequences = []
            for configuration in configurations:
                sequence = tuple(
                    (line.reader_id.id, int(line.port_no or 0))
                    for line in configuration.antenna_sequence_ids.sorted("sequence")
                )
                lane_sequences.append((configuration, sequence))
            for index, (first_lane, first_sequence) in enumerate(lane_sequences):
                for second_lane, second_sequence in lane_sequences[index + 1:]:
                    def is_subsequence(smaller, larger):
                        position = 0
                        for item in larger:
                            if position < len(smaller) and item == smaller[position]:
                                position += 1
                        return position == len(smaller)
                    if is_subsequence(first_sequence, second_sequence) or is_subsequence(second_sequence, first_sequence):
                        raise UserError(_(
                            "Logical Lanes %(first)s and %(second)s have ambiguous Antenna Sequences."
                        ) % {"first": first_lane.display_name, "second": second_lane.display_name})
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
            stale.mapped("layout_lane_ids.antenna_sequence_ids").unlink()
            stale.mapped("layout_lane_ids.reader_config_ids").unlink()
            stale.mapped("layout_lane_ids").write({"active": False})
            stale._apply_parking_state_transition("blocked", force=True)
        # Lane Master is intentionally untouched: a Parking Layout never owns or
        # cascade-deletes stable Lane identity.
        return len(stale)

    @staticmethod
    def _normalize_sync_code(value):
        return str(value or "").strip().upper()

    def _derive_parking_runtime_assembly(self, areas, whitelist_by_code, edge_by_code):
        """Derive physical runtime assembly from contextual Lane payloads.

        Cloud intentionally does not publish a separate ``controllers`` tree. Server,
        Controller and Reader are independent identities; each Lane Configuration owns
        the runtime association through ``server_code`` / ``controller_code`` /
        ``readers``. Edge materializes that association only for acquisition/runtime.
        """
        self.ensure_one()
        controller_specs = {}
        reader_specs = {}

        for area in areas:
            if not isinstance(area, dict):
                raise UserError(_("Parking Runtime parking_areas must contain objects."))
            lanes = area.get("lanes") or []
            if not isinstance(lanes, list):
                raise UserError(_("Parking Layout lanes must be an array."))
            for lane in lanes:
                if not isinstance(lane, dict):
                    raise UserError(_("Parking Layout lanes must contain objects."))
                lane_code = self._normalize_sync_code(lane.get("lane_code")) or "-"
                server_code = self._normalize_sync_code(lane.get("server_code"))
                controller_code = self._normalize_sync_code(lane.get("controller_code"))
                if not server_code or not controller_code:
                    raise UserError(_(
                        "Lane %(lane)s requires Server Code and Controller Code."
                    ) % {"lane": lane_code})

                server_identity = whitelist_by_code.get(server_code)
                controller_identity = whitelist_by_code.get(controller_code)
                edge = edge_by_code.get(server_code)
                if (
                    not edge or not server_identity
                    or server_identity.device_type_code != "SERVER"
                ):
                    raise UserError(_(
                        "Lane %(lane)s references an invalid Server identity %(server)s."
                    ) % {"lane": lane_code, "server": server_code})
                if (
                    not controller_identity
                    or controller_identity.device_type_code != "CONTROLLER"
                    or not controller_identity.active
                ):
                    raise UserError(_(
                        "Lane %(lane)s references an invalid Controller identity %(controller)s."
                    ) % {"lane": lane_code, "controller": controller_code})

                controller_spec = controller_specs.get(controller_code)
                if controller_spec and controller_spec["server_code"] != server_code:
                    raise UserError(_(
                        "Controller %(controller)s is assigned to multiple Servers in Parking Runtime."
                    ) % {"controller": controller_code})
                if not controller_spec:
                    controller_specs[controller_code] = {
                        "server_code": server_code,
                        "edge": edge,
                        "identity": controller_identity,
                    }

                lane_readers = lane.get("readers") or []
                if not isinstance(lane_readers, list):
                    raise UserError(_("Lane Readers must be an array."))
                for reader_item in lane_readers:
                    if not isinstance(reader_item, dict):
                        raise UserError(_("Lane Reader payload must contain objects."))
                    reader_code = self._normalize_sync_code(reader_item.get("technical_code"))
                    serial = str(reader_item.get("serial_number") or "").strip().upper()
                    identity = whitelist_by_code.get(reader_code)
                    if (
                        not reader_code or not serial or not identity
                        or identity.device_type_code != "RFID_READER"
                        or not identity.active
                    ):
                        raise UserError(_(
                            "Lane %(lane)s references an invalid RFID Reader identity."
                        ) % {"lane": lane_code})
                    identity_serial = str(identity.serial_number or "").strip().upper()
                    if identity_serial and identity_serial != serial:
                        raise UserError(_(
                            "RFID Reader %(reader)s Serial does not match Device Whitelist."
                        ) % {"reader": reader_code})

                    parameters = reader_item.get("reader_parameters") or {}
                    if not isinstance(parameters, dict) or set(parameters) - {
                        "power_dbm", "read_interval_ms", "tid_start_address", "tid_length",
                    }:
                        raise UserError(_("Invalid Reader Parameters payload."))
                    try:
                        config = {
                            "power_dbm": int(
                                parameters.get("power_dbm")
                                if parameters.get("power_dbm") is not None else 30
                            ),
                            "read_interval_ms": int(parameters.get("read_interval_ms") or 200),
                            "tid_start_address": int(parameters.get("tid_start_address") or 0),
                            "tid_length": int(parameters.get("tid_length") or 4),
                        }
                    except (TypeError, ValueError) as exc:
                        raise UserError(_("Invalid Reader Parameters.")) from exc
                    if (
                        config["power_dbm"] < 0 or config["power_dbm"] > 40
                        or config["read_interval_ms"] <= 0
                        or config["read_interval_ms"] > 60000
                        or config["tid_start_address"] < 0
                        or config["tid_length"] <= 0
                    ):
                        raise UserError(_("Reader Parameters are outside the supported range."))

                    ports = reader_item.get("ports") or []
                    if not isinstance(ports, list) or not ports:
                        raise UserError(_(
                            "Published RFID Reader %(reader)s has no Reader Ports."
                        ) % {"reader": reader_code})
                    port_numbers = set()
                    for port in ports:
                        if not isinstance(port, dict) or set(port) - {"port_no"}:
                            raise UserError(_("Reader Port payload contains unsupported fields."))
                        try:
                            port_no = int(port.get("port_no") or 0)
                        except (TypeError, ValueError) as exc:
                            raise UserError(_("Reader Port No. must be an integer.")) from exc
                        if not 1 <= port_no <= 16 or port_no in port_numbers:
                            raise UserError(_("Reader Port must be unique and between 1 and 16."))
                        port_numbers.add(port_no)

                    previous = reader_specs.get(reader_code)
                    if previous:
                        if previous["controller_code"] != controller_code:
                            raise UserError(_(
                                "RFID Reader %(reader)s is assigned to multiple Controllers in Parking Runtime."
                            ) % {"reader": reader_code})
                        if previous["serial"] != serial:
                            raise UserError(_(
                                "RFID Reader %(reader)s has conflicting Serial Numbers in Parking Runtime."
                            ) % {"reader": reader_code})
                        if previous["config"] != config:
                            raise UserError(_(
                                "RFID Reader %(reader)s has conflicting runtime parameters across Lane Configurations."
                            ) % {"reader": reader_code})
                        # Multiple logical Lanes may use different subsets of ports
                        # from the same physical Reader. The Controller must enable
                        # the union while each Lane keeps its own Antenna Sequence.
                        previous["ports"].update(port_numbers)
                    else:
                        reader_specs[reader_code] = {
                            "controller_code": controller_code,
                            "identity": identity,
                            "serial": serial,
                            "name": str(reader_item.get("reader_name") or identity.name or serial).strip(),
                            "config": config,
                            "ports": set(port_numbers),
                        }

        return controller_specs, reader_specs

    def _verify_parking_runtime_projection(self, areas, snapshot_revision):
        """Verify Lane Master + contextual Lane Configuration persistence."""
        self.ensure_one()
        Parking = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        LaneMaster = self.env["nsp.parking.lane"].sudo().with_context(active_test=False)
        LayoutLane = self.env["nsp.parking.layout.lane"].sudo().with_context(active_test=False)

        expected_areas = {}
        for item in areas or []:
            if not isinstance(item, dict):
                raise UserError(_("Parking Runtime parking_areas must contain objects."))
            area_code = self._normalize_sync_code(item.get("parking_area_code"))
            if not area_code or area_code in expected_areas:
                raise UserError(_("Parking Runtime Parking Area Code is required and must be unique."))
            expected_areas[area_code] = item

        Parking.flush_model([
            "code", "name", "branch_id", "state", "published_revision",
            "runtime_snapshot_revision", "runtime_synced_at",
        ])
        LaneMaster.flush_model(["code", "name", "branch_id", "active"])
        LayoutLane.flush_model([
            "parking_area_id", "lane_id", "edge_server_id", "controller_id", "active",
        ])
        self.env["nsp.parking.layout.lane.reader.config"].sudo().flush_model()
        self.env["nsp.parking.layout.lane.reader.port"].sudo().flush_model()
        self.env["nsp.parking.layout.lane.sequence"].sudo().flush_model()

        persisted = Parking.search([("code", "in", list(expected_areas))]) if expected_areas else Parking.browse()
        by_code = {self._normalize_sync_code(row.code): row for row in persisted}
        missing = sorted(set(expected_areas) - set(by_code))
        if missing:
            raise UserError(_("Parking Runtime apply did not persist Parking Layout(s): %s") % ", ".join(missing))

        verified_lane_count = 0
        verified_reader_count = 0
        verified_sequence_count = 0
        for area_code, item in expected_areas.items():
            parking = by_code[area_code]
            expected_branch = self._normalize_sync_code(item.get("branch_code"))
            expected_state = str(item.get("state") or "draft").strip().lower()
            expected_published_revision = int(item.get("published_revision") or 0)
            if self._normalize_sync_code(parking.branch_id.code) != expected_branch:
                raise UserError(_("Parking Layout %s was persisted under the wrong Branch.") % area_code)
            if parking.state != expected_state:
                raise UserError(_("Parking Layout %s was persisted with the wrong State.") % area_code)
            if int(parking.published_revision or 0) != expected_published_revision:
                raise UserError(_("Parking Layout %s Published Revision mismatch.") % area_code)
            if int(parking.runtime_snapshot_revision or 0) != int(snapshot_revision or 0):
                raise UserError(_("Parking Layout %s Runtime Snapshot Revision mismatch.") % area_code)

            expected_lanes = {}
            for lane_item in item.get("lanes") or []:
                lane_code = self._normalize_sync_code(lane_item.get("lane_code"))
                if not lane_code or lane_code in expected_lanes:
                    raise UserError(_("Parking Lane Code is required and must be unique inside a Layout."))
                expected_lanes[lane_code] = lane_item

            configurations = LayoutLane.search([
                ("parking_area_id", "=", parking.id), ("active", "=", True),
            ])
            by_lane_code = {
                self._normalize_sync_code(row.lane_id.code): row for row in configurations
            }
            if set(by_lane_code) != set(expected_lanes):
                raise UserError(_("Parking Layout %s Lane Configuration persistence mismatch.") % area_code)

            for lane_code, lane_item in expected_lanes.items():
                configuration = by_lane_code[lane_code]
                lane_master = configuration.lane_id
                if self._normalize_sync_code(lane_master.code) != lane_code:
                    raise UserError(_("Lane Master %s was not persisted correctly.") % lane_code)
                if lane_master.branch_id != parking.branch_id:
                    raise UserError(_("Lane Master %s was persisted under the wrong Branch.") % lane_code)
                expected_controller = self._normalize_sync_code(lane_item.get("controller_code"))
                expected_server = self._normalize_sync_code(lane_item.get("server_code"))
                if self._normalize_sync_code(configuration.controller_id.controller_id) != expected_controller:
                    raise UserError(_("Lane Configuration %s was persisted with the wrong Controller.") % lane_code)
                if self._normalize_sync_code(configuration.edge_server_id.edge_server_code) != expected_server:
                    raise UserError(_("Lane Configuration %s was persisted with the wrong Server.") % lane_code)

                expected_readers = {}
                for reader in lane_item.get("readers") or []:
                    reader_code = self._normalize_sync_code(reader.get("technical_code"))
                    expected_readers[reader_code] = {
                        int(port.get("port_no") or 0) for port in (reader.get("ports") or [])
                    }
                persisted_readers = {
                    self._normalize_sync_code(config.reader_id.device_code): set(config.port_ids.mapped("port_no"))
                    for config in configuration.reader_config_ids
                }
                if expected_readers != persisted_readers:
                    raise UserError(_("Lane Configuration %s Device Configuration was not persisted completely.") % lane_code)

                expected_sequence = [
                    (
                        self._normalize_sync_code(row.get("reader_code")),
                        int(row.get("port_no") or 0),
                        float(row.get("duration_from_previous_seconds") or 0.0),
                    )
                    for row in sorted(
                        lane_item.get("antenna_sequence") or [],
                        key=lambda row: int(row.get("sequence") or 0),
                    )
                ]
                persisted_sequence = [
                    (
                        self._normalize_sync_code(row.reader_id.device_code),
                        int(row.port_no or 0),
                        float(row.duration_from_previous or 0.0),
                    )
                    for row in configuration.antenna_sequence_ids.sorted("sequence")
                ]
                if expected_sequence != persisted_sequence:
                    raise UserError(_("Lane Configuration %s Antenna Sequence was not persisted correctly.") % lane_code)

                verified_lane_count += 1
                verified_reader_count += len(persisted_readers)
                verified_sequence_count += len(persisted_sequence)

        return {
            "parking_area_ids": persisted.ids,
            "parking_area_codes": sorted(by_code),
            "parking_area_count": len(persisted),
            "lane_count": verified_lane_count,
            "reader_config_count": verified_reader_count,
            "sequence_point_count": verified_sequence_count,
        }

    def _parking_runtime_projection_matches_payload(self, areas):
        """Return True only when the local Edge projection already matches Cloud.

        This deliberately ignores the top-level Sync Job revision. That revision is
        transport metadata and can drift ahead of the actual business projection
        after a failed/partial historical deployment. An older incoming revision is
        safe to skip only when every Parking Layout and contextual Lane Configuration
        represented by that Cloud response is already present locally.
        """
        self.ensure_one()
        Parking = self.env["nsp.parking.area"].sudo().with_context(active_test=False)
        LayoutLane = self.env["nsp.parking.layout.lane"].sudo().with_context(active_test=False)

        expected = {}
        for item in areas or []:
            if not isinstance(item, dict):
                return False
            area_code = self._normalize_sync_code(item.get("parking_area_code"))
            if not area_code or area_code in expected:
                return False
            expected[area_code] = item

        # An authoritative empty Cloud projection matches only when Edge has no
        # non-blocked Parking Layout projection left.
        if not expected:
            return not bool(Parking.search_count([("state", "!=", "blocked")]))

        persisted = Parking.search([("code", "in", list(expected))])
        by_code = {self._normalize_sync_code(row.code): row for row in persisted}
        if set(by_code) != set(expected):
            return False

        for area_code, item in expected.items():
            parking = by_code[area_code]
            try:
                published_revision = int(item.get("published_revision") or 0)
            except (TypeError, ValueError):
                return False
            if int(parking.published_revision or 0) != published_revision:
                return False
            if parking.state != str(item.get("state") or "draft").strip().lower():
                return False
            if self._normalize_sync_code(parking.branch_id.code) != self._normalize_sync_code(item.get("branch_code")):
                return False

            expected_lanes = {
                self._normalize_sync_code(lane.get("lane_code"))
                for lane in (item.get("lanes") or [])
                if isinstance(lane, dict) and lane.get("lane_code")
            }
            configs = LayoutLane.search([
                ("parking_area_id", "=", parking.id),
                ("active", "=", True),
            ])
            actual_lanes = {
                self._normalize_sync_code(row.lane_id.code)
                for row in configs if row.lane_id and row.lane_id.code
            }
            if actual_lanes != expected_lanes:
                return False

            # Presence of Lane codes alone is insufficient. Device Configuration
            # and Antenna Sequence must both exist with the same cardinality as Cloud.
            by_lane = {self._normalize_sync_code(row.lane_id.code): row for row in configs}
            for lane_item in item.get("lanes") or []:
                lane_code = self._normalize_sync_code(lane_item.get("lane_code"))
                config = by_lane.get(lane_code)
                if not config:
                    return False
                if len(config.reader_config_ids) != len(lane_item.get("readers") or []):
                    return False
                if len(config.antenna_sequence_ids) != len(lane_item.get("antenna_sequence") or []):
                    return False
        return True

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

        areas = data.get("parking_areas") or []
        branches = data.get("branches") or []
        whitelist = data.get("device_whitelist") or []
        for name, value in (
            ("parking_areas", areas),
            ("branches", branches),
            ("device_whitelist", whitelist),
        ):
            if not isinstance(value, list):
                raise UserError(_("Parking Runtime %s must be an array.") % name)

        current_revision = int(self.snapshot_revision or 0)
        recovery_replay = False
        if revision < current_revision:
            if self._parking_runtime_projection_matches_payload(areas):
                return {
                    "applied": 0,
                    "removed": 0,
                    "revision": revision,
                    "current_revision": current_revision,
                    "stale": True,
                    "recovery_replay": False,
                }
            # Cloud is source of truth. If the transport revision says Edge is
            # newer but the corresponding business projection is missing or
            # incomplete, replay the authoritative Cloud snapshot and repair the
            # drift instead of returning a false-success stale skip.
            recovery_replay = True

        with self.env.cr.savepoint():
            Branch = self.env["nsp.branch"].sudo().with_context(active_test=False)
            existing_branches = {record.code: record for record in Branch.search([])}
            incoming_branch_codes = set()
            for item in branches:
                if not isinstance(item, dict) or set(item) - {
                    "branch_code", "branch_name", "timezone", "active",
                }:
                    raise UserError(_("Invalid Branch payload."))
                code = self._normalize_sync_code(item.get("branch_code"))
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
                self._normalize_sync_code(record.technical_code): record
                for record in Whitelist.search([])
                if record.technical_code
            }

            # Materialize Server identities first. Controller/Reader association is
            # derived below from contextual Lane Configuration payloads, not from a
            # separate Cloud topology tree.
            Edge = self.env["nsp.edge.server"].sudo().with_context(active_test=False)
            edge_by_code = {
                self._normalize_sync_code(record.edge_server_code): record
                for record in Edge.search([])
                if record.edge_server_code
            }
            for identity in whitelist:
                if self._normalize_sync_code(identity.get("device_type_code")) != "SERVER":
                    continue
                code = self._normalize_sync_code(identity.get("technical_code"))
                whitelist_record = whitelist_by_code.get(code)
                if not code or not whitelist_record or whitelist_record.device_type_code != "SERVER":
                    raise UserError(_("Published Server identity is invalid."))
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

            controller_specs, reader_specs = self._derive_parking_runtime_assembly(
                areas, whitelist_by_code, edge_by_code
            )

            Controller = self.env["nsp.controller"].sudo().with_context(active_test=False)
            controllers = Controller.search([])
            controller_by_code = {
                self._normalize_sync_code(record.controller_id): record
                for record in controllers if record.controller_id
            }
            for controller_code, spec in controller_specs.items():
                identity = spec["identity"]
                values = {
                    "controller_name": identity.name or controller_code,
                    "active": bool(identity.active),
                    "cloud_removed": False,
                    "whitelist_id": identity.id,
                }
                controller = controller_by_code.get(controller_code)
                if controller:
                    self._write_changed(controller, values)
                else:
                    controller = Controller.create({"controller_id": controller_code, **values})
                    controller_by_code[controller_code] = controller

            Device = self.env["nsp.device"].sudo().with_context(active_test=False)
            readers = Device.search([])
            reader_by_code = {}
            duplicate_reader_codes = set()
            for reader in readers:
                code = self._normalize_sync_code(reader.device_code)
                if not code:
                    continue
                if code in reader_by_code and reader_by_code[code] != reader:
                    duplicate_reader_codes.add(code)
                else:
                    reader_by_code[code] = reader
            if duplicate_reader_codes:
                raise UserError(_(
                    "Duplicate RFID Reader Code(s) exist on Edge: %s"
                ) % ", ".join(sorted(duplicate_reader_codes)))
            reader_by_serial = {
                str(record.serial_number or "").strip().upper(): record
                for record in readers if record.serial_number
            }
            declared_ports_by_reader = {}
            declared_config_by_reader = {}
            for reader_code, spec in reader_specs.items():
                controller = controller_by_code.get(spec["controller_code"])
                if not controller:
                    raise UserError(_(
                        "Controller %(controller)s could not be materialized for Reader %(reader)s."
                    ) % {"controller": spec["controller_code"], "reader": reader_code})
                by_code = reader_by_code.get(reader_code)
                by_serial = reader_by_serial.get(spec["serial"])
                if by_code and by_serial and by_code != by_serial:
                    raise UserError(_(
                        "RFID Reader identity conflict: Code %(code)s and Serial %(serial)s "
                        "belong to different Edge records."
                    ) % {"code": reader_code, "serial": spec["serial"]})
                config = spec["config"]
                values = {
                    "name": spec["name"],
                    "serial_number": spec["serial"],
                    "device_code": reader_code,
                    "whitelist_id": spec["identity"].id,
                    "active": bool(spec["identity"].active),
                    "cloud_removed": False,
                }
                reader = by_code or by_serial
                if reader:
                    previous_code = self._normalize_sync_code(reader.device_code)
                    previous_serial = str(reader.serial_number or "").strip().upper()
                    self._write_changed(reader, values)
                    if previous_code and previous_code != reader_code and reader_by_code.get(previous_code) == reader:
                        reader_by_code.pop(previous_code, None)
                    if previous_serial and previous_serial != spec["serial"] and reader_by_serial.get(previous_serial) == reader:
                        reader_by_serial.pop(previous_serial, None)
                else:
                    reader = Device.create(values)
                reader_by_code[reader_code] = reader
                reader_by_serial[spec["serial"]] = reader
                declared_ports_by_reader[reader_code] = set(spec["ports"])
                declared_config_by_reader[reader_code] = dict(config)

            parking_sync = self.with_context(
                nsp_declared_reader_ports={
                    code: sorted(ports)
                    for code, ports in declared_ports_by_reader.items()
                },
                nsp_declared_reader_configs=declared_config_by_reader,
            )
            for area in areas:
                parking_sync._apply_parking_config(area, snapshot_revision=revision)
            removed_area = self._reconcile_parking_config_snapshot(areas)
            self._validate_operational_parking_topology()
            verification = self._verify_parking_runtime_projection(areas, revision)
            self.write({
                "snapshot_revision": revision,
                "last_pull_at": fields.Datetime.now(),
                "sync_cursor": False,
            })

        return {
            "applied": len(areas) + len(whitelist),
            "removed": removed_area,
            "revision": revision,
            "previous_revision": current_revision,
            "stale": False,
            "recovery_replay": recovery_replay,
            **verification,
        }

