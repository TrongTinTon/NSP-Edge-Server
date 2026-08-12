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

export class NspDeviceTreeView extends Component {
    static template = "nsp_business_gatekeeper.DeviceTreeView";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            selectedKey: null,
            expanded: {},
            calibrationNodes: [],
            calibrationLoaded: false,
            calibrationLoadError: false,
        });

        onMounted(async () => {
            if (this.mode === "lane_calibration") {
                await this.refreshCalibrationTree();
            }
        });
    }

    async refreshCalibrationTree() {
        if (this.mode !== "lane_calibration") {
            return;
        }
        const sessionId = Number(this.props.record?.resId || 0);
        if (!sessionId) {
            this.state.calibrationNodes = [];
            this.state.calibrationLoaded = true;
            this.state.calibrationLoadError = false;
            return;
        }
        try {
            this.state.calibrationNodes = await this.orm.searchRead(
                "nsp.measurement.device.node",
                [["session_id", "=", sessionId]],
                [
                    "source_node_id",
                    "device_type",
                    "parent_id",
                    "sequence",
                    "server_id",
                    "controller_id",
                    "reader_id",
                    "device_name",
                    "device_status",
                    "serial_number",
                    "port_numbers",
                    "power_dbm",
                    "read_interval_ms",
                    "tid_addr",
                    "tid_len",
                ],
                { order: "sequence,id" }
            );
            this.state.calibrationLoadError = false;
        } catch (error) {
            console.error("Unable to load Lane Calibration Device Configuration", error);
            this.state.calibrationNodes = [];
            this.state.calibrationLoadError = true;
        } finally {
            this.state.calibrationLoaded = true;
        }
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
        if (status === "online") {
            return "Online";
        }
        if (status === "degraded") {
            return "Degraded";
        }
        if (status === "offline") {
            return "Offline";
        }
        return "Unknown";
    }

    _parkingLaneEntries() {
        const data = this.props.record?.data || {};
        const lines = data.reader_config_ids?.records || [];
        const server = {
            id: many2oneId(data.edge_server_id) || "server",
            name: data.edge_server_name || many2oneLabel(data.edge_server_id, "Server"),
            status: this._status(data.edge_server_status),
        };
        const controller = {
            id: many2oneId(data.controller_id) || "controller",
            name: data.controller_name || many2oneLabel(data.controller_id, "Controller"),
            status: this._status(data.controller_status),
        };
        return lines.map((line, index) => {
            const row = line.data || {};
            return {
                key: `lane-reader-${line.resId || line.id || index}`,
                server,
                controller,
                reader: {
                    id: many2oneId(row.reader_id) || `reader-${index}`,
                    name: row.reader_name || many2oneLabel(row.reader_id, "Reader"),
                    serial: row.reader_serial_number || "—",
                    status: this._status(row.reader_status),
                },
                ports: row.port_summary || "—",
                values: {
                    power: Number(row.power_dbm || 0),
                    interval: Number(row.read_interval_ms || 0),
                    tidStart: Number(row.tid_start_address || 0),
                    tidLength: Number(row.tid_length || 0),
                },
            };
        });
    }

    _calibrationEntries() {
        const records = this.state.calibrationNodes || [];
        const byNodeId = new Map(
            records
                .map((row) => [Number(row.id || 0), row])
                .filter(([nodeId]) => Boolean(nodeId))
        );
        const entries = [];
        for (const row of records) {
            if (row.device_type !== "reader") {
                continue;
            }
            const controllerRow = byNodeId.get(many2oneId(row.parent_id)) || {};
            const serverRow = byNodeId.get(many2oneId(controllerRow.parent_id)) || {};
            if (controllerRow.device_type !== "controller" || serverRow.device_type !== "server") {
                continue;
            }
            const index = entries.length;
            entries.push({
                key: `cal-reader-${row.id || index}`,
                nodeId: Number(row.id || 0),
                server: {
                    nodeId: Number(serverRow.id || 0),
                    id: many2oneId(serverRow.server_id) || `server-${index}`,
                    name: serverRow.device_name || many2oneLabel(serverRow.server_id, "Server"),
                    status: this._status(serverRow.device_status),
                },
                controller: {
                    nodeId: Number(controllerRow.id || 0),
                    id: many2oneId(controllerRow.controller_id) || `controller-${index}`,
                    name: controllerRow.device_name || many2oneLabel(controllerRow.controller_id, "Controller"),
                    status: this._status(controllerRow.device_status),
                },
                reader: {
                    nodeId: Number(row.id || 0),
                    id: many2oneId(row.reader_id) || `reader-${index}`,
                    name: row.device_name || many2oneLabel(row.reader_id, "Reader"),
                    serial: row.serial_number || "—",
                    status: this._status(row.device_status),
                },
                ports: row.port_numbers || "—",
                values: {
                    power: Number(row.power_dbm || 0),
                    interval: Number(row.read_interval_ms || 0),
                    tidStart: Number(row.tid_addr || 0),
                    tidLength: Number(row.tid_len || 0),
                },
            });
        }
        return entries;
    }

    get entries() {
        if (this.mode === "parking_lane") {
            return this._parkingLaneEntries();
        }
        if (this.mode === "lane_calibration") {
            return this._calibrationEntries();
        }
        return [];
    }

    get tree() {
        const groups = new Map();
        if (this.mode === "lane_calibration") {
            const records = this.state.calibrationNodes || [];
            const byNodeId = new Map(
                records
                    .map((row) => [Number(row.id || 0), row])
                    .filter(([nodeId]) => Boolean(nodeId))
            );

            for (const row of records.filter((node) => node.device_type === "server")) {
                const serverId = many2oneId(row.server_id) || Number(row.id || 0);
                const serverKey = `server-${serverId}`;
                groups.set(serverKey, {
                    key: serverKey,
                    nodeId: Number(row.id || 0),
                    id: serverId,
                    name: row.device_name || many2oneLabel(row.server_id, "Server"),
                    status: this._status(row.device_status),
                    controllers: new Map(),
                });
            }

            for (const row of records.filter((node) => node.device_type === "controller")) {
                const serverRow = byNodeId.get(many2oneId(row.parent_id));
                if (!serverRow || serverRow.device_type !== "server") {
                    continue;
                }
                const serverId = many2oneId(serverRow.server_id) || Number(serverRow.id || 0);
                const serverKey = `server-${serverId}`;
                if (!groups.has(serverKey)) {
                    groups.set(serverKey, {
                        key: serverKey,
                        nodeId: Number(serverRow.id || 0),
                        id: serverId,
                        name: serverRow.device_name || many2oneLabel(serverRow.server_id, "Server"),
                        status: this._status(serverRow.device_status),
                        controllers: new Map(),
                    });
                }
                const controllerId = many2oneId(row.controller_id) || Number(row.id || 0);
                const controllerKey = `${serverKey}-controller-${controllerId}`;
                groups.get(serverKey).controllers.set(controllerKey, {
                    key: controllerKey,
                    nodeId: Number(row.id || 0),
                    id: controllerId,
                    name: row.device_name || many2oneLabel(row.controller_id, "Controller"),
                    status: this._status(row.device_status),
                    readers: [],
                });
            }
        }
        if (this.mode === "parking_lane") {
            const data = this.props.record?.data || {};
            const serverId = many2oneId(data.edge_server_id);
            const controllerId = many2oneId(data.controller_id);
            if (serverId) {
                const serverKey = `server-${serverId}`;
                const server = {
                    key: serverKey,
                    id: serverId,
                    name: data.edge_server_name || many2oneLabel(data.edge_server_id, "Server"),
                    status: this._status(data.edge_server_status),
                    controllers: new Map(),
                };
                groups.set(serverKey, server);
                if (controllerId) {
                    const controllerKey = `${serverKey}-controller-${controllerId}`;
                    server.controllers.set(controllerKey, {
                        key: controllerKey,
                        id: controllerId,
                        name: data.controller_name || many2oneLabel(data.controller_id, "Controller"),
                        status: this._status(data.controller_status),
                        readers: [],
                    });
                }
            }
        }

        for (const entry of this.entries) {
            const serverKey = `server-${entry.server.id}`;
            if (!groups.has(serverKey)) {
                groups.set(serverKey, {
                    key: serverKey,
                    ...entry.server,
                    controllers: new Map(),
                });
            }
            const server = groups.get(serverKey);
            const controllerKey = `${serverKey}-controller-${entry.controller.id}`;
            if (!server.controllers.has(controllerKey)) {
                server.controllers.set(controllerKey, {
                    key: controllerKey,
                    ...entry.controller,
                    readers: [],
                });
            }
            server.controllers.get(controllerKey).readers.push(entry);
        }

        return [...groups.values()].map((server) => ({
            ...server,
            controllers: [...server.controllers.values()],
        }));
    }

    toggleExpanded(key) {
        this.state.expanded[key] = !this.isExpanded(key);
    }

    isExpanded(key) {
        return this.state.expanded[key] !== false;
    }

    selectReader(entry) {
        this.state.selectedKey = entry.key;
    }

    get selectedEntry() {
        const entries = this.entries;
        return entries.find((entry) => entry.key === this.state.selectedKey) || entries[0] || null;
    }

    get breadcrumb() {
        const entry = this.selectedEntry;
        if (!entry) {
            return "";
        }
        return `${entry.server.name} / ${entry.controller.name} / ${entry.reader.name}`;
    }
}

registry.category("fields").add("nsp_device_tree_view", {
    component: NspDeviceTreeView,
    supportedTypes: ["boolean"],
});
