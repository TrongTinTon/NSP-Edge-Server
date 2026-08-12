/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useX2ManyCrud } from "@web/views/fields/relational_utils";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { SelectCreateDialog } from "@web/views/view_dialogs/select_create_dialog";

const STATUS_ONLINE = new Set(["online"]);
const STATUS_OFFLINE = new Set(["offline", "error", "block", "revoked"]);
const STATUS_DEGRADED = new Set(["degraded"]);

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

function toMany2one(option) {
    return option ? { id: option.id, display_name: option.name } : false;
}

export class NspDeviceTreeView extends Component {
    static template = "nsp_master_gatekeeper.DeviceTreeView";
    static props = { ...standardFieldProps };

    setup() {
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.orm = useService("orm");

        // Lane Calibration persists nodes directly. Lane Setup and Lane Configuration use
        // their own contextual Reader x2many while sharing this exact Tree UI.
        const { removeRecord } = useX2ManyCrud(() => this.flatReaderList, false);
        this.removeFlatReaderRecord = removeRecord;

        this.state = useState({
            selectedKey: null,
            expanded: {},
            editing: false,
            saving: false,
            configState: "Saved",
            draft: {},
            errors: {},
            portEditingKey: null,
            portDraft: "",
            portError: "",
            runtimeStatus: {},
            masterMeta: {},
            calibrationNodes: [],
            calibrationPorts: [],
            calibrationLoaded: false,
        });
        this.statusRefreshTimer = null;

        onMounted(async () => {
            if (this.mode === "lane_calibration") {
                await this.refreshCalibrationTree();
            }
            await this.refreshOperationalStatuses();
            this.statusRefreshTimer = setInterval(() => {
                this.refreshOperationalStatuses();
            }, 5000);
        });

        onWillUnmount(() => {
            if (this.statusRefreshTimer) {
                clearInterval(this.statusRefreshTimer);
                this.statusRefreshTimer = null;
            }
        });
    }

    get mode() {
        const model = this.props.record?.resModel;
        if (model === "nsp.parking.layout.lane") {
            return "parking_lane";
        }
        if (model === "nsp.lane.setup.wizard") {
            return "lane_setup";
        }
        if (model === "nsp.measurement.session") {
            return "lane_calibration";
        }
        return "unsupported";
    }

    get editable() {
        const data = this.props.record?.data || {};
        if (this.mode === "parking_lane") {
            return !data.parking_area_state || data.parking_area_state === "draft";
        }
        if (this.mode === "lane_setup") {
            return true;
        }
        if (this.mode === "lane_calibration") {
            return Boolean(data.device_configuration_editable);
        }
        return false;
    }

    get flatReaderList() {
        if (this.mode === "parking_lane") {
            return this.props.record?.data?.reader_config_ids || null;
        }
        if (this.mode === "lane_setup") {
            return this.props.record?.data?.device_line_ids || null;
        }
        return null;
    }

    get topologyEditable() {
        if (!this.editable) {
            return false;
        }
        if (this.mode === "lane_setup") {
            return this.props.record?.data?.source_scope !== "calibration";
        }
        return true;
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

    _statusFor(kind, id, fallbackValue) {
        const numericId = Number(id || 0);
        const liveValue = numericId ? this.state.runtimeStatus[`${kind}:${numericId}`] : false;
        return this._status(liveValue || fallbackValue);
    }

    _masterMeta(kind, id) {
        const numericId = Number(id || 0);
        return numericId ? (this.state.masterMeta[`${kind}:${numericId}`] || {}) : {};
    }

    _masterName(kind, id, fallback) {
        return this._masterMeta(kind, id).name || fallback;
    }

    _masterSerial(id, fallback) {
        return this._masterMeta("reader", id).serial || fallback;
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

    async _calibrationSessionId({ createIfMissing = false } = {}) {
        let sessionId = Number(this.props.record?.resId || 0);
        if (sessionId || !createIfMissing) {
            return sessionId;
        }
        const saved = await this.props.record.save();
        sessionId = Number(this.props.record?.resId || 0);
        if (!saved || !sessionId) {
            throw new Error("Save the Lane Calibration before adding Device Configuration.");
        }
        return sessionId;
    }

    async refreshCalibrationTree() {
        if (this.mode !== "lane_calibration") {
            return;
        }
        const sessionId = await this._calibrationSessionId();
        if (!sessionId) {
            this.state.calibrationNodes = [];
            this.state.calibrationPorts = [];
            this.state.calibrationLoaded = true;
            return;
        }
        try {
            const nodes = await this.orm.searchRead(
                "nsp.measurement.device.node",
                [["session_id", "=", sessionId]],
                [
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
            const nodeIds = nodes.map((node) => Number(node.id)).filter(Boolean);
            const ports = nodeIds.length
                ? await this.orm.searchRead(
                    "nsp.measurement.reader.port",
                    [["reader_node_id", "in", nodeIds]],
                    ["reader_node_id", "port_no", "sequence"],
                    { order: "sequence,port_no,id" }
                )
                : [];
            this.state.calibrationNodes = nodes;
            this.state.calibrationPorts = ports;
            this.state.calibrationLoaded = true;
        } catch (error) {
            this.notification.add(error?.message || "Unable to load Device Tree.", {
                title: "Device Tree",
                type: "danger",
            });
        }
    }

    get calibrationNodes() {
        return this.state.calibrationNodes || [];
    }

    _calibrationNode(nodeId) {
        const numericId = Number(nodeId || 0);
        return this.calibrationNodes.find((node) => Number(node.id) === numericId) || null;
    }

    _nodeMasterId(node) {
        if (!node) {
            return false;
        }
        if (node.device_type === "server") {
            return many2oneId(node.server_id);
        }
        if (node.device_type === "controller") {
            return many2oneId(node.controller_id);
        }
        if (node.device_type === "reader") {
            return many2oneId(node.reader_id);
        }
        return false;
    }

    _nodeView(node) {
        if (!node) {
            return null;
        }
        const type = node.device_type;
        const id = this._nodeMasterId(node);
        const labelFallback = node.device_name || many2oneLabel(
            type === "server" ? node.server_id : type === "controller" ? node.controller_id : node.reader_id,
            type === "server" ? "Server" : type === "controller" ? "Controller" : "Reader"
        );
        return {
            key: `cal-node-${node.id}`,
            nodeId: Number(node.id),
            parentNodeId: many2oneId(node.parent_id),
            type,
            id,
            name: this._masterName(type, id, labelFallback),
            status: this._statusFor(type, id, node.device_status),
            serial: type === "reader"
                ? this._masterSerial(id, node.serial_number || "—")
                : "",
            raw: node,
        };
    }

    _portsForReaderNode(nodeId) {
        const numericId = Number(nodeId || 0);
        return (this.state.calibrationPorts || [])
            .filter((port) => many2oneId(port.reader_node_id) === numericId)
            .map((port) => ({
                key: `port-${port.id}`,
                id: Number(port.id),
                portNo: Number(port.port_no || 0),
                sequence: Number(port.sequence || 10),
            }))
            .filter((port) => port.portNo > 0)
            .sort((a, b) => a.portNo - b.portNo || a.id - b.id);
    }

    _entryForReaderNode(readerNode) {
        const reader = this._nodeView(readerNode);
        if (!reader) {
            return null;
        }
        const parent = this._calibrationNode(reader.parentNodeId);
        const controllerNode = parent?.device_type === "controller" ? parent : null;
        const controller = controllerNode ? this._nodeView(controllerNode) : {
            key: "unassigned-controller",
            nodeId: false,
            id: false,
            name: "Unassigned Controller",
            status: "unknown",
        };
        const grandparent = controllerNode ? this._calibrationNode(many2oneId(controllerNode.parent_id)) : null;
        const serverNode = grandparent?.device_type === "server" ? grandparent : null;
        const server = serverNode ? this._nodeView(serverNode) : {
            key: "unassigned-server",
            nodeId: false,
            id: false,
            name: "Unassigned Server",
            status: "unknown",
        };
        return {
            key: reader.key,
            nodeId: reader.nodeId,
            rawNode: readerNode,
            server,
            controller,
            reader,
            ports: readerNode.port_numbers || "—",
            values: {
                power: Number(readerNode.power_dbm || 0),
                interval: Number(readerNode.read_interval_ms || 0),
                tidStart: Number(readerNode.tid_addr || 0),
                tidLength: Number(readerNode.tid_len || 0),
            },
        };
    }

    _flatLaneEntries() {
        const data = this.props.record?.data || {};
        const lines = this.flatReaderList?.records || [];
        const serverId = many2oneId(data.edge_server_id);
        const controllerId = many2oneId(data.controller_id);
        const server = {
            key: `server-${serverId || "unassigned"}`,
            id: serverId,
            name: this._masterName(
                "server",
                serverId,
                data.edge_server_name || many2oneLabel(data.edge_server_id, "Server")
            ),
            status: this._statusFor("server", serverId, data.edge_server_status),
        };
        const controller = {
            key: `${server.key}-controller-${controllerId || "unassigned"}`,
            id: controllerId,
            name: this._masterName(
                "controller",
                controllerId,
                data.controller_name || many2oneLabel(data.controller_id, "Controller")
            ),
            status: this._statusFor("controller", controllerId, data.controller_status),
        };
        return lines.map((line, index) => {
            const lineData = line.data || {};
            const readerId = many2oneId(lineData.reader_id);
            return {
                key: `${this.mode}-reader-${line.resId || line.id || index}`,
                record: line,
                server,
                controller,
                reader: {
                    key: `${this.mode}-reader-master-${readerId || index}`,
                    id: readerId,
                    name: this._masterName(
                        "reader",
                        readerId,
                        lineData.reader_name || many2oneLabel(lineData.reader_id, "Reader")
                    ),
                    serial: this._masterSerial(readerId, lineData.reader_serial_number || "—"),
                    status: this._statusFor("reader", readerId, lineData.reader_status),
                },
                ports: lineData.port_summary || "—",
                values: {
                    power: Number(lineData.power_dbm || 0),
                    interval: Number(lineData.read_interval_ms || 0),
                    tidStart: Number(lineData.tid_start_address || 0),
                    tidLength: Number(lineData.tid_length || 0),
                },
            };
        });
    }

    get entries() {
        if (this.mode === "parking_lane" || this.mode === "lane_setup") {
            return this._flatLaneEntries();
        }
        if (this.mode === "lane_calibration") {
            return this.calibrationNodes
                .filter((node) => node.device_type === "reader")
                .map((node) => this._entryForReaderNode(node))
                .filter(Boolean);
        }
        return [];
    }

    get tree() {
        if (this.mode === "parking_lane" || this.mode === "lane_setup") {
            return this._flatLaneTree();
        }
        if (this.mode === "lane_calibration") {
            return this._calibrationTree();
        }
        return [];
    }

    _flatLaneTree() {
        const data = this.props.record?.data || {};
        const serverId = many2oneId(data.edge_server_id);
        if (!serverId) {
            return [];
        }
        const server = {
            key: `server-${serverId}`,
            nodeId: false,
            id: serverId,
            name: this._masterName("server", serverId, data.edge_server_name || many2oneLabel(data.edge_server_id, "Server")),
            status: this._statusFor("server", serverId, data.edge_server_status),
            controllers: [],
        };
        const controllerId = many2oneId(data.controller_id);
        if (controllerId) {
            server.controllers.push({
                key: `${server.key}-controller-${controllerId}`,
                nodeId: false,
                id: controllerId,
                name: this._masterName("controller", controllerId, data.controller_name || many2oneLabel(data.controller_id, "Controller")),
                status: this._statusFor("controller", controllerId, data.controller_status),
                readers: this.entries,
            });
        }
        return [server];
    }

    _calibrationTree() {
        const serverNodes = this.calibrationNodes.filter((node) => node.device_type === "server");
        const controllerNodes = this.calibrationNodes.filter((node) => node.device_type === "controller");
        const readerNodes = this.calibrationNodes.filter((node) => node.device_type === "reader");

        const result = serverNodes.map((serverNode) => {
            const server = this._nodeView(serverNode);
            const controllers = controllerNodes
                .filter((controllerNode) => many2oneId(controllerNode.parent_id) === server.nodeId)
                .map((controllerNode) => {
                    const controller = this._nodeView(controllerNode);
                    const readers = readerNodes
                        .filter((readerNode) => many2oneId(readerNode.parent_id) === controller.nodeId)
                        .map((readerNode) => this._entryForReaderNode(readerNode));
                    return { ...controller, readers };
                });
            return { ...server, controllers };
        });
        return result;
    }

    get canAddServer() {
        if (!this.topologyEditable) {
            return false;
        }
        if (this.mode === "lane_calibration") {
            return true;
        }
        return !many2oneId(this.props.record?.data?.edge_server_id);
    }

    canAddController(server) {
        if (!this.topologyEditable || !server) {
            return false;
        }
        if (this.mode === "lane_calibration") {
            return true;
        }
        return !many2oneId(this.props.record?.data?.controller_id);
    }

    get selectedEntry() {
        return this.entries.find((entry) => entry.key === this.state.selectedKey)
            || this.entries[0]
            || null;
    }

    get selectedPorts() {
        if (this.mode !== "lane_calibration" || !this.selectedEntry?.nodeId) {
            return [];
        }
        return this._portsForReaderNode(this.selectedEntry.nodeId);
    }

    get breadcrumb() {
        const entry = this.selectedEntry;
        if (!entry) {
            return "No Reader selected";
        }
        return `${entry.server.name} > ${entry.controller.name} > ${entry.reader.name}`;
    }

    isExpanded(key) {
        return this.state.expanded[key] !== false;
    }

    toggleExpanded(key) {
        this.state.expanded[key] = !this.isExpanded(key);
    }

    selectReader(entry) {
        this.state.selectedKey = entry.key;
        this.state.editing = false;
        this.state.saving = false;
        this.state.configState = "Saved";
        this.state.draft = {};
        this.state.errors = {};
        this.cancelPortEdit();
    }

    _openMasterSearch({ title, resModel, domain, fields, labelField, onSelected }) {
        this.dialog.add(SelectCreateDialog, {
            title,
            resModel,
            domain,
            context: {},
            multiSelect: false,
            noCreate: true,
            onSelected: async (resIds) => {
                const resId = Number(resIds?.[0] || 0);
                if (!resId) {
                    return;
                }
                const rows = await this.orm.searchRead(
                    resModel,
                    [["id", "=", resId]],
                    fields,
                    { limit: 1 }
                );
                const row = rows[0];
                if (!row) {
                    throw new Error(`The selected ${title.toLowerCase()} is no longer available.`);
                }
                await onSelected({
                    id: row.id,
                    name: row[labelField] || row.name || `Record ${row.id}`,
                }, row);
            },
        });
    }

    _masterDomain(deviceType, excludeIds = []) {
        const code = {
            server: "SERVER",
            controller: "CONTROLLER",
            reader: "RFID_READER",
        }[deviceType];
        const domain = [
            ["active", "=", true],
            ["whitelist_id", "!=", false],
            ["whitelist_id.active", "=", true],
            ["whitelist_id.device_type_code", "=", code],
        ];
        if (excludeIds.length) {
            domain.push(["id", "not in", excludeIds]);
        }
        return domain;
    }

    _existingMasterIds(deviceType, exceptNodeId = false) {
        if (this.mode !== "lane_calibration") {
            return [];
        }
        return this.calibrationNodes
            .filter((node) => node.device_type === deviceType && Number(node.id) !== Number(exceptNodeId || 0))
            .map((node) => this._nodeMasterId(node))
            .filter(Boolean);
    }

    _openServerSearch({ title, onSelected, exceptNodeId = false }) {
        this._openMasterSearch({
            title,
            resModel: "nsp.edge.server",
            domain: this._masterDomain("server", this._existingMasterIds("server", exceptNodeId)),
            fields: ["name", "status"],
            labelField: "name",
            onSelected,
        });
    }

    _openControllerSearch({ title, onSelected, exceptNodeId = false }) {
        this._openMasterSearch({
            title,
            resModel: "nsp.controller",
            domain: this._masterDomain("controller", this._existingMasterIds("controller", exceptNodeId)),
            fields: ["controller_name", "status"],
            labelField: "controller_name",
            onSelected,
        });
    }

    _openReaderSearch({ title, onSelected, exceptNodeId = false }) {
        this._openMasterSearch({
            title,
            resModel: "nsp.device",
            domain: this._masterDomain("reader", this._existingMasterIds("reader", exceptNodeId)),
            fields: ["name", "serial_number", "status"],
            labelField: "name",
            onSelected,
        });
    }

    async _createCalibrationNode(deviceType, masterId, parentNodeId = false) {
        const sessionId = await this._calibrationSessionId({ createIfMissing: true });
        const fieldName = {
            server: "server_id",
            controller: "controller_id",
            reader: "reader_id",
        }[deviceType];
        if (!fieldName) {
            throw new Error("Unsupported Device Tree node type.");
        }
        const result = await this.orm.create("nsp.measurement.device.node", [{
            session_id: sessionId,
            device_type: deviceType,
            [fieldName]: Number(masterId),
            parent_id: Number(parentNodeId || 0) || false,
        }]);
        const nodeId = Number(Array.isArray(result) ? result[0] : result || 0);
        if (!nodeId) {
            throw new Error(`Unable to persist ${deviceType} node.`);
        }
        await this.refreshCalibrationTree();
        await this.refreshOperationalStatuses();
        return nodeId;
    }

    async addServer() {
        if (!this.topologyEditable || !this.canAddServer) {
            return;
        }
        this._openServerSearch({
            title: "Add Server",
            onSelected: async (server) => {
                if (this.mode !== "lane_calibration") {
                    await this.props.record.update({ edge_server_id: toMany2one(server) });
                    return;
                }
                const nodeId = await this._createCalibrationNode("server", server.id);
                this.state.expanded = { ...this.state.expanded, [`cal-node-${nodeId}`]: true };
            },
        });
    }

    async addController(server = null) {
        if (!this.topologyEditable || !server) {
            return;
        }
        if (this.mode === "lane_calibration" && !server.nodeId) {
            return;
        }
        this._openControllerSearch({
            title: "Add Controller",
            onSelected: async (controller) => {
                if (this.mode !== "lane_calibration") {
                    await this.props.record.update({ controller_id: toMany2one(controller) });
                    return;
                }
                await this._createCalibrationNode("controller", controller.id, server.nodeId);
                this.state.expanded = { ...this.state.expanded, [server.key]: true };
            },
        });
    }

    async addReader(server = null, controller = null) {
        if (!this.topologyEditable || !controller) {
            return;
        }
        if (this.mode === "lane_calibration" && !controller.nodeId) {
            return;
        }
        if (this.mode !== "lane_calibration" && (!server || !this.flatReaderList)) {
            return;
        }
        this._openReaderSearch({
            title: "Add Reader",
            onSelected: async (reader) => {
                if (this.mode === "lane_calibration") {
                    const nodeId = await this._createCalibrationNode("reader", reader.id, controller.nodeId);
                    this.state.expanded = { ...this.state.expanded, [controller.key]: true };
                    this.state.selectedKey = `cal-node-${nodeId}`;
                    return;
                }
                await this._createFlatReader({ server, controller, reader });
            },
        });
    }

    async _createFlatReader({ server, controller, reader }) {
        const list = this.flatReaderList;
        if (!list) {
            return;
        }
        await this.props.record.update({
            edge_server_id: toMany2one(server),
            controller_id: toMany2one(controller),
        });
        const line = await list.addNewRecord({
            context: list.context || {},
            mode: "edit",
            position: "bottom",
        });
        await line.update({ reader_id: toMany2one(reader) });
        this.state.selectedKey = `lane-reader-${line.resId || line.id}`;
        await this.refreshOperationalStatuses();
    }

    async editServer(server) {
        if (!server || !this.topologyEditable) {
            return;
        }
        this._openServerSearch({
            title: "Edit Server",
            exceptNodeId: server.nodeId,
            onSelected: async (selection) => {
                if (this.mode !== "lane_calibration") {
                    await this.props.record.update({ edge_server_id: toMany2one(selection) });
                    return;
                }
                await this.orm.write("nsp.measurement.device.node", [server.nodeId], {
                    server_id: selection.id,
                });
                await this.refreshCalibrationTree();
                await this.refreshOperationalStatuses();
            },
        });
    }

    async editController(server, controller) {
        if (!controller || !this.topologyEditable) {
            return;
        }
        this._openControllerSearch({
            title: "Edit Controller",
            exceptNodeId: controller.nodeId,
            onSelected: async (selection) => {
                if (this.mode !== "lane_calibration") {
                    await this.props.record.update({ controller_id: toMany2one(selection) });
                    return;
                }
                await this.orm.write("nsp.measurement.device.node", [controller.nodeId], {
                    controller_id: selection.id,
                });
                await this.refreshCalibrationTree();
                await this.refreshOperationalStatuses();
            },
        });
    }

    async editMapping(entry) {
        if (!entry || !this.topologyEditable) {
            return;
        }
        this._openReaderSearch({
            title: "Edit Reader",
            exceptNodeId: entry.nodeId,
            onSelected: async (reader) => {
                if (this.mode !== "lane_calibration") {
                    await entry.record.update({ reader_id: toMany2one(reader) });
                    await this.refreshOperationalStatuses();
                    return;
                }
                await this.orm.write("nsp.measurement.device.node", [entry.nodeId], {
                    reader_id: reader.id,
                });
                await this.refreshCalibrationTree();
                await this.refreshOperationalStatuses();
                this.state.configState = "Modified";
            },
        });
    }

    deleteServer(server) {
        if (!server || !this.topologyEditable) {
            return;
        }
        const body = `Remove ${server.name} and its contextual child mappings from this Device Tree?`;
        this.dialog.add(ConfirmationDialog, {
            title: "Remove Server",
            body,
            confirmLabel: "Remove",
            confirm: async () => {
                if (this.mode === "lane_calibration") {
                    await this.orm.unlink("nsp.measurement.device.node", [server.nodeId]);
                    await this.refreshCalibrationTree();
                    this._clearSelectionIfMissing();
                } else {
                    const records = this.entries.map((entry) => entry.record);
                    await Promise.all(records.map((record) => this.removeFlatReaderRecord(record)));
                    await this.props.record.update({ edge_server_id: false, controller_id: false });
                    this.state.selectedKey = null;
                }
            },
        });
    }

    deleteController(server, controller) {
        if (!controller || !this.topologyEditable) {
            return;
        }
        const body = `Remove ${controller.name} and its contextual Reader mappings from this Device Tree?`;
        this.dialog.add(ConfirmationDialog, {
            title: "Remove Controller",
            body,
            confirmLabel: "Remove",
            confirm: async () => {
                if (this.mode === "lane_calibration") {
                    await this.orm.unlink("nsp.measurement.device.node", [controller.nodeId]);
                    await this.refreshCalibrationTree();
                    this._clearSelectionIfMissing();
                } else {
                    const records = this.entries.map((entry) => entry.record);
                    await Promise.all(records.map((record) => this.removeFlatReaderRecord(record)));
                    await this.props.record.update({ controller_id: false });
                    this.state.selectedKey = null;
                }
            },
        });
    }

    deleteMapping(entry) {
        if (!entry || !this.topologyEditable) {
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: "Remove Reader",
            body: `Remove ${entry.reader.name} from this Device Tree?`,
            confirmLabel: "Remove",
            confirm: async () => {
                if (this.mode === "lane_calibration") {
                    await this.orm.unlink("nsp.measurement.device.node", [entry.nodeId]);
                    await this.refreshCalibrationTree();
                } else {
                    await this.removeFlatReaderRecord(entry.record);
                }
                this.state.selectedKey = null;
                this.cancelEdit();
            },
        });
    }

    _clearSelectionIfMissing() {
        if (this.state.selectedKey && !this.entries.some((entry) => entry.key === this.state.selectedKey)) {
            this.state.selectedKey = null;
            this.cancelEdit();
        }
    }

    async addPort() {
        if (this.mode !== "lane_calibration" || !this.editable || !this.selectedEntry?.nodeId) {
            return;
        }
        const used = new Set(this.selectedPorts.map((port) => port.portNo));
        let nextPort = 1;
        while (nextPort <= 16 && used.has(nextPort)) {
            nextPort += 1;
        }
        if (nextPort > 16) {
            this.notification.add("All Reader Ports P1-P16 are already configured.", {
                title: "No available Port",
                type: "warning",
            });
            return;
        }
        await this.orm.create("nsp.measurement.reader.port", [{
            reader_node_id: this.selectedEntry.nodeId,
            port_no: nextPort,
        }]);
        await this.refreshCalibrationTree();
        this.state.configState = "Modified";
    }

    startPortEdit(port) {
        if (!port || this.mode !== "lane_calibration" || !this.editable) {
            return;
        }
        this.state.portEditingKey = port.key;
        this.state.portDraft = String(port.portNo);
        this.state.portError = "";
    }

    cancelPortEdit() {
        this.state.portEditingKey = null;
        this.state.portDraft = "";
        this.state.portError = "";
    }

    onPortInput(ev) {
        this.state.portDraft = ev.target.value || "";
        this.state.portError = "";
    }

    async savePort(port) {
        if (!port || this.mode !== "lane_calibration" || !this.editable) {
            return;
        }
        const value = Number(this.state.portDraft);
        if (!Number.isInteger(value) || value < 1 || value > 16) {
            this.state.portError = "Port must be an integer from 1 to 16.";
            return;
        }
        if (this.selectedPorts.some((item) => item.id !== port.id && item.portNo === value)) {
            this.state.portError = `P${value} is already configured for this Reader.`;
            return;
        }
        await this.orm.write("nsp.measurement.reader.port", [port.id], { port_no: value });
        await this.refreshCalibrationTree();
        this.cancelPortEdit();
        this.state.configState = "Modified";
    }

    deletePort(port) {
        if (!port || this.mode !== "lane_calibration" || !this.editable) {
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: "Remove Port",
            body: `Remove P${port.portNo} from ${this.selectedEntry?.reader?.name || "this Reader"}?`,
            confirmLabel: "Remove",
            confirm: async () => {
                await this.orm.unlink("nsp.measurement.reader.port", [port.id]);
                await this.refreshCalibrationTree();
                this.cancelPortEdit();
                this.state.configState = "Modified";
            },
        });
    }

    startEdit() {
        const entry = this.selectedEntry;
        if (!entry || !this.editable) {
            return;
        }
        this.state.draft = { ...entry.values };
        this.state.errors = {};
        this.state.editing = true;
        this.state.configState = "Editing";
    }

    cancelEdit() {
        this.state.editing = false;
        this.state.saving = false;
        this.state.draft = {};
        this.state.errors = {};
        this.state.configState = "Saved";
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
        if (!Number.isFinite(power) || power < 0 || power > 40) {
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

    _configurationValuesFromDraft() {
        const draft = this.state.draft;
        if (this.mode === "parking_lane" || this.mode === "lane_setup") {
            return {
                power_dbm: Number(draft.power),
                read_interval_ms: Number(draft.interval),
                tid_start_address: Number(draft.tidStart),
                tid_length: Number(draft.tidLength),
            };
        }
        return {
            power_dbm: Number(draft.power),
            read_interval_ms: Number(draft.interval),
            tid_addr: Number(draft.tidStart),
            tid_len: Number(draft.tidLength),
        };
    }

    async _persistCalibrationConfiguration(entry, values) {
        const sessionId = await this._calibrationSessionId();
        const nodeId = Number(entry?.nodeId || 0);
        if (!sessionId || !nodeId) {
            throw new Error("Reader node must be persisted before its configuration can be saved.");
        }
        const canonical = await this.orm.call(
            "nsp.measurement.session",
            "action_save_device_configuration",
            [[sessionId]],
            {
                node_id: nodeId,
                values,
                port_numbers: this.selectedPorts.map((port) => Number(port.portNo)),
            }
        );
        if (!canonical || Number(canonical.id || 0) !== nodeId) {
            throw new Error("The Reader configuration save was not confirmed.");
        }
        await this.refreshCalibrationTree();
        return true;
    }

    async saveConfiguration() {
        const entry = this.selectedEntry;
        if (!entry || this.state.saving || !this._validateDraft()) {
            return;
        }
        this.state.saving = true;
        this.state.configState = "Saving...";
        const values = this._configurationValuesFromDraft();
        try {
            if (this.mode === "lane_calibration") {
                await this._persistCalibrationConfiguration(entry, values);
            } else {
                const wasPersisted = Boolean(entry.record.resId);
                await entry.record.update(values);
                const saved = wasPersisted
                    ? await entry.record.save({ reload: false })
                    : await this.props.record.save();
                if (!saved) {
                    throw new Error("The Reader configuration save was rejected.");
                }
            }
            await this.refreshOperationalStatuses();
            this.state.editing = false;
            this.state.configState = "Saved";
            this.state.draft = {};
            this.state.errors = {};
            this.notification.add("Reader configuration saved.", {
                title: "Configuration saved",
                type: "success",
            });
        } catch (error) {
            this.state.configState = "Save failed";
            this.notification.add(error?.message || "Unable to update Reader configuration.", {
                title: "Configuration error",
                type: "danger",
            });
        } finally {
            this.state.saving = false;
        }
    }

    async refreshOperationalStatuses() {
        const serverIds = new Set();
        const controllerIds = new Set();
        const readerIds = new Set();
        const data = this.props.record?.data || {};

        if (this.mode === "parking_lane" || this.mode === "lane_setup") {
            const serverId = many2oneId(data.edge_server_id);
            const controllerId = many2oneId(data.controller_id);
            if (serverId) {
                serverIds.add(serverId);
            }
            if (controllerId) {
                controllerIds.add(controllerId);
            }
            for (const entry of this._flatLaneEntries()) {
                if (entry.reader.id) {
                    readerIds.add(Number(entry.reader.id));
                }
            }
        } else if (this.mode === "lane_calibration") {
            for (const node of this.calibrationNodes) {
                const id = this._nodeMasterId(node);
                if (!id) {
                    continue;
                }
                if (node.device_type === "server") {
                    serverIds.add(id);
                } else if (node.device_type === "controller") {
                    controllerIds.add(id);
                } else if (node.device_type === "reader") {
                    readerIds.add(id);
                }
            }
        }

        const queries = [];
        if (serverIds.size) {
            queries.push(this.orm.read("nsp.edge.server", [...serverIds], ["name", "status"])
                .then((rows) => ["server", rows]));
        }
        if (controllerIds.size) {
            queries.push(this.orm.read("nsp.controller", [...controllerIds], ["controller_name", "status"])
                .then((rows) => ["controller", rows]));
        }
        if (readerIds.size) {
            queries.push(this.orm.read("nsp.device", [...readerIds], ["name", "serial_number", "status"])
                .then((rows) => ["reader", rows]));
        }
        if (!queries.length) {
            return;
        }

        try {
            const nextStatus = { ...this.state.runtimeStatus };
            const nextMeta = { ...this.state.masterMeta };
            for (const [kind, rows] of await Promise.all(queries)) {
                for (const row of rows || []) {
                    if (!row?.id) {
                        continue;
                    }
                    const key = `${kind}:${row.id}`;
                    nextStatus[key] = row.status || "unknown";
                    if (kind === "server") {
                        nextMeta[key] = { name: row.name || `Server ${row.id}` };
                    } else if (kind === "controller") {
                        nextMeta[key] = { name: row.controller_name || `Controller ${row.id}` };
                    } else {
                        nextMeta[key] = {
                            name: row.name || row.serial_number || `Reader ${row.id}`,
                            serial: row.serial_number || "—",
                        };
                    }
                }
            }
            this.state.runtimeStatus = nextStatus;
            this.state.masterMeta = nextMeta;
        } catch {
            // Keep the last known status if a transient refresh fails.
        }
    }
}

registry.category("fields").add("nsp_device_tree_view", {
    component: NspDeviceTreeView,
    supportedTypes: ["boolean"],
});
