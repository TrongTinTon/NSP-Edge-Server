/** @odoo-module **/

import { Component, onMounted, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const STATUS_ONLINE = new Set(["online"]);
const STATUS_DEGRADED = new Set(["degraded"]);
const STATUS_OFFLINE = new Set(["offline", "error", "block", "revoked"]);

function many2oneId(value) {
    if (Array.isArray(value)) {
        return Number(value[0] || 0) || false;
    }
    if (value && typeof value === "object") {
        return Number(value.id || value.resId || 0) || false;
    }
    return Number(value || 0) || false;
}

function many2oneLabel(value, fallback) {
    if (Array.isArray(value)) {
        return value[1] || fallback;
    }
    if (value && typeof value === "object") {
        return value.display_name || value.name || fallback;
    }
    return fallback;
}

function antennaNumbers(value) {
    const matches = String(value || "").match(/\d+/g) || [];
    return [...new Set(matches.map(Number).filter((number) => number > 0))].sort((a, b) => a - b);
}

export class NspDeviceTreeView extends Component {
    static template = "nsp_business_gatekeeper.DeviceTreeView";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({
            selectedKey: null,
            expanded: {},
            calibrationNodes: [],
            parkingReaders: [],
            loaded: false,
            editing: false,
            saving: false,
            draft: {},
            errors: {},
        });

        onMounted(async () => {
            if (this.mode === "lane_calibration") {
                await this.refreshCalibrationTree();
            } else if (this.mode === "parking_lane") {
                await this.refreshParkingLaneReaders();
            } else {
                this.state.loaded = true;
            }
        });
    }

    get mode() {
        const model = this.props.record?.resModel;
        if (model === "nsp.parking.layout.lane") {
            return "parking_lane";
        }
        if (model === "nsp.measurement.session") {
            return "lane_calibration";
        }
        return "unsupported";
    }

    get editable() {
        return this.mode === "lane_calibration"
            && Boolean(this.props.record?.data?.device_configuration_editable);
    }

    _status(value) {
        const status = String(value || "").toLowerCase();
        if (STATUS_ONLINE.has(status)) {
            return "online";
        }
        if (STATUS_DEGRADED.has(status)) {
            return "degraded";
        }
        if (STATUS_OFFLINE.has(status)) {
            return "offline";
        }
        return "unknown";
    }

    statusLabel(status) {
        return {
            online: "Online",
            degraded: "Degraded",
            offline: "Offline",
        }[status] || "Unknown";
    }

    async refreshCalibrationTree() {
        const sessionId = Number(this.props.record?.resId || 0);
        if (!sessionId) {
            this.state.loaded = true;
            return;
        }
        try {
            const nodes = await this.orm.searchRead(
                "nsp.measurement.device.node",
                [["session_id", "=", sessionId]],
                [
                    "device_type", "parent_id", "sequence",
                    "controller_id", "reader_id",
                    "device_name", "device_status", "serial_number", "port_numbers",
                    "runtime_override_enabled",
                    "effective_power_dbm", "effective_read_interval_ms",
                    "effective_tid_addr", "effective_tid_len",
                ],
                { order: "sequence,id" }
            );
            this.state.calibrationNodes = nodes;
        } finally {
            this.state.loaded = true;
        }
    }

    async refreshParkingLaneReaders() {
        const layoutLaneId = Number(this.props.record?.resId || 0);
        if (!layoutLaneId) {
            this.state.loaded = true;
            return;
        }
        try {
            this.state.parkingReaders = await this.orm.searchRead(
                "nsp.parking.layout.lane.reader.config",
                [["layout_lane_id", "=", layoutLaneId]],
                [
                    "reader_id", "reader_name", "reader_serial_number", "reader_status",
                    "port_summary", "power_dbm", "read_interval_ms",
                    "tid_start_address", "tid_length",
                ],
                { order: "reader_id,id" }
            );
        } finally {
            this.state.loaded = true;
        }
    }

    _calibrationNodeMap() {
        return new Map(
            this.state.calibrationNodes
                .map((node) => [Number(node.id || 0), node])
                .filter(([id]) => Boolean(id))
        );
    }

    _calibrationEntries() {
        const byId = this._calibrationNodeMap();
        return this.state.calibrationNodes
            .filter((node) => node.device_type === "reader")
            .map((readerNode, index) => {
                const controllerNode = byId.get(many2oneId(readerNode.parent_id));
                if (controllerNode?.device_type !== "controller") {
                    return null;
                }
                return {
                    key: `cal-reader-${readerNode.id || index}`,
                    nodeId: Number(readerNode.id || 0),
                    controller: {
                        nodeId: Number(controllerNode.id || 0),
                        id: many2oneId(controllerNode.controller_id),
                        name: controllerNode.device_name || many2oneLabel(controllerNode.controller_id, "Controller"),
                        status: this._status(controllerNode.device_status),
                    },
                    reader: {
                        id: many2oneId(readerNode.reader_id),
                        name: readerNode.device_name || many2oneLabel(readerNode.reader_id, "Reader"),
                        serial: readerNode.serial_number || "—",
                        status: this._status(readerNode.device_status),
                    },
                    portNumbers: antennaNumbers(readerNode.port_numbers),
                    runtimeOverride: Boolean(readerNode.runtime_override_enabled),
                    values: {
                        power: Number(readerNode.effective_power_dbm || 0),
                        interval: Number(readerNode.effective_read_interval_ms || 0),
                        tidStart: Number(readerNode.effective_tid_addr || 0),
                        tidLength: Number(readerNode.effective_tid_len || 0),
                    },
                };
            })
            .filter(Boolean);
    }

    _parkingEntries() {
        const data = this.props.record?.data || {};
        const controller = {
            id: many2oneId(data.controller_id),
            name: data.controller_name || many2oneLabel(data.controller_id, "Controller"),
            status: this._status(data.controller_status),
        };
        return this.state.parkingReaders.map((row, index) => ({
            key: `lane-reader-${row.id || index}`,
            controller,
            reader: {
                id: many2oneId(row.reader_id),
                name: row.reader_name || many2oneLabel(row.reader_id, "Reader"),
                serial: row.reader_serial_number || "—",
                status: this._status(row.reader_status),
            },
            portNumbers: antennaNumbers(row.port_summary),
            values: {
                power: Number(row.power_dbm || 0),
                interval: Number(row.read_interval_ms || 0),
                tidStart: Number(row.tid_start_address || 0),
                tidLength: Number(row.tid_length || 0),
            },
        }));
    }

    get entries() {
        if (this.mode === "lane_calibration") {
            return this._calibrationEntries();
        }
        if (this.mode === "parking_lane") {
            return this._parkingEntries();
        }
        return [];
    }

    get tree() {
        if (this.mode === "unsupported") {
            return [];
        }
        if (this.mode === "lane_calibration") {
            const nodes = this.state.calibrationNodes;
            const entriesByController = new Map();
            for (const entry of this.entries) {
                const key = Number(entry.controller.nodeId || 0);
                if (!entriesByController.has(key)) {
                    entriesByController.set(key, []);
                }
                entriesByController.get(key).push(entry);
            }
            return nodes
                .filter((node) => node.device_type === "controller")
                .map((controllerNode) => ({
                    key: `cal-controller-${controllerNode.id}`,
                    nodeId: Number(controllerNode.id || 0),
                    name: controllerNode.device_name || many2oneLabel(controllerNode.controller_id, "Controller"),
                    status: this._status(controllerNode.device_status),
                    readers: entriesByController.get(Number(controllerNode.id)) || [],
                }));
        }

        return [];
    }

    get parkingControllers() {
        if (this.mode !== "parking_lane") {
            return [];
        }
        const first = this.entries[0];
        if (!first) {
            return [];
        }
        return [{
            key: `controller-${first.controller.id || first.controller.name}`,
            ...first.controller,
            readers: this.entries,
        }];
    }

    get selectedEntry() {
        return this.entries.find((entry) => entry.key === this.state.selectedKey) || this.entries[0] || null;
    }

    get selectedAntennaNumbers() {
        if (!this.selectedEntry) {
            return [];
        }
        return this.selectedEntry.portNumbers || [];
    }

    get breadcrumb() {
        const entry = this.selectedEntry;
        if (!entry) {
            return "";
        }
        return `${entry.controller.name} > ${entry.reader.name}`;
    }

    isExpanded(key) {
        return this.state.expanded[key] !== false;
    }

    toggleExpanded(key) {
        this.state.expanded[key] = !this.isExpanded(key);
    }

    selectReader(entry) {
        this.state.selectedKey = entry.key;
        this.cancelEdit();
    }

    startEdit() {
        const entry = this.selectedEntry;
        if (!entry || !this.editable) {
            return;
        }
        this.state.draft = { ...entry.values };
        this.state.errors = {};
        this.state.editing = true;
    }

    cancelEdit() {
        this.state.editing = false;
        this.state.saving = false;
        this.state.draft = {};
        this.state.errors = {};
    }

    onConfigInput(ev) {
        const field = ev.target.dataset.field;
        if (!field) {
            return;
        }
        this.state.draft[field] = ev.target.value;
        if (this.state.errors[field]) {
            delete this.state.errors[field];
        }
    }

    _validateDraft() {
        const draft = this.state.draft;
        const errors = {};
        const power = Number(draft.power);
        const interval = Number(draft.interval);
        const tidStart = Number(draft.tidStart);
        const tidLength = Number(draft.tidLength);
        if (!Number.isInteger(power) || power < 0 || power > 40) {
            errors.power = "Power must be between 0 and 40 dBm.";
        }
        if (!Number.isInteger(interval) || interval < 1 || interval > 60000) {
            errors.interval = "Read Interval must be between 1 and 60000 ms.";
        }
        if (!Number.isInteger(tidStart) || tidStart < 0) {
            errors.tidStart = "TID Start cannot be negative.";
        }
        if (!Number.isInteger(tidLength) || tidLength < 1) {
            errors.tidLength = "TID Length must be greater than zero.";
        }
        this.state.errors = errors;
        return Object.keys(errors).length === 0;
    }

    async saveConfiguration() {
        const entry = this.selectedEntry;
        const sessionId = Number(this.props.record?.resId || 0);
        if (!entry?.nodeId || !sessionId || this.state.saving || !this._validateDraft()) {
            return;
        }
        this.state.saving = true;
        try {
            await this.orm.call(
                "nsp.measurement.session",
                "action_save_device_configuration",
                [[sessionId]],
                {
                    node_id: Number(entry.nodeId),
                    values: {
                        power_dbm: Number(this.state.draft.power),
                        read_interval_ms: Number(this.state.draft.interval),
                        tid_addr: Number(this.state.draft.tidStart),
                        tid_len: Number(this.state.draft.tidLength),
                    },
                }
            );
            await this.refreshCalibrationTree();
            this.cancelEdit();
            this.notification.add("Reader runtime settings applied.", {
                title: "Lane Calibration", type: "success",
            });
        } catch (error) {
            this.notification.add(error?.message || "Unable to update Reader runtime settings.", {
                title: "Lane Calibration", type: "danger",
            });
        } finally {
            this.state.saving = false;
        }
    }

    async resetConfiguration() {
        const entry = this.selectedEntry;
        const sessionId = Number(this.props.record?.resId || 0);
        if (!entry?.nodeId || !sessionId || this.state.saving) {
            return;
        }
        this.state.saving = true;
        try {
            await this.orm.call(
                "nsp.measurement.session",
                "action_reset_device_configuration",
                [[sessionId]],
                { node_id: Number(entry.nodeId) }
            );
            await this.refreshCalibrationTree();
            this.cancelEdit();
            this.notification.add("Cloud Reader settings restored.", {
                title: "Lane Calibration", type: "success",
            });
        } catch (error) {
            this.notification.add(error?.message || "Unable to reset Reader runtime settings.", {
                title: "Lane Calibration", type: "danger",
            });
        } finally {
            this.state.saving = false;
        }
    }
}

registry.category("fields").add("nsp_device_tree_view", {
    component: NspDeviceTreeView,
    supportedTypes: ["boolean"],
});
