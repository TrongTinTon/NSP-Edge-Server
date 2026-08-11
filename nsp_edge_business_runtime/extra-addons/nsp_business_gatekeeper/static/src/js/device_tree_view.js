/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useX2ManyCrud } from "@web/views/fields/relational_utils";
import { SelectCreateDialog } from "@web/views/view_dialogs/select_create_dialog";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const STATUS_ONLINE = new Set(["online"]);
const STATUS_OFFLINE = new Set(["offline", "error", "block", "revoked", "degraded"]);

function many2oneId(value) {
    if (Array.isArray(value)) {
        return value[0] || false;
    }
    if (value && typeof value === "object") {
        return value.id || value.resId || false;
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
    static template = "nsp_business_gatekeeper.DeviceTreeView";
    static props = { ...standardFieldProps };

    setup() {
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.orm = useService("orm");
        const { removeRecord } = useX2ManyCrud(() => this.deviceList, false);
        this.removeDeviceRecord = removeRecord;
        const { removeRecord: removePortRecord } = useX2ManyCrud(
            () => this.selectedPortList,
            false
        );
        this.removePortRecord = removePortRecord;
        this.state = useState({
            query: "",
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
        });
    }

    get mode() {
        const model = this.props.record?.resModel;
        if (model === "nsp.parking.lane") {
            return "parking_lane";
        }
        if (model === "nsp.measurement.session") {
            return "lane_calibration";
        }
        return "unsupported";
    }

    get editable() {
        // Edge is a runtime projection. Device Configuration is managed on
        // Cloud (nsp_master_gatekeeper) and is always read-only here.
        return false;
    }

    get deviceList() {
        const data = this.props.record?.data || {};
        if (this.mode === "parking_lane") {
            return data.reader_config_ids;
        }
        if (this.mode === "lane_calibration") {
            return data.reader_line_ids;
        }
        return null;
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

    _parkingLaneEntries() {
        const data = this.props.record?.data || {};
        const lines = data.reader_config_ids?.records || [];
        const server = {
            id: many2oneId(data.edge_server_id) || "server",
            name: many2oneLabel(data.edge_server_id, "Server"),
            status: this._status(data.edge_server_status),
        };
        const controller = {
            id: many2oneId(data.controller_id) || "controller",
            name: many2oneLabel(data.controller_id, "Controller"),
            status: this._status(data.controller_status),
        };
        return lines.map((line, index) => {
            const lineData = line.data || {};
            return {
                key: `lane-reader-${line.resId || line.id || index}`,
                record: line,
                server,
                controller,
                reader: {
                    id: many2oneId(lineData.reader_id) || `reader-${index}`,
                    name: lineData.reader_name || many2oneLabel(lineData.reader_id, "Reader"),
                    serial: lineData.reader_serial_number || "—",
                    status: this._status(lineData.reader_status),
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

    _calibrationEntries() {
        const lines = this.props.record?.data?.reader_line_ids?.records || [];
        return lines.map((line, index) => {
            const data = line.data || {};
            return {
                key: `cal-reader-${line.resId || line.id || index}`,
                record: line,
                server: {
                    id: many2oneId(data.edge_server_id) || `server-${index}`,
                    name: many2oneLabel(data.edge_server_id, "Server"),
                    status: this._status(data.edge_server_status),
                },
                controller: {
                    id: many2oneId(data.controller_id) || `controller-${index}`,
                    name: many2oneLabel(data.controller_id, "Controller"),
                    status: this._status(data.controller_status),
                },
                reader: {
                    id: many2oneId(data.reader_id) || `reader-${index}`,
                    name: data.reader_name || many2oneLabel(data.reader_id, "Reader"),
                    serial: data.serial_number || "—",
                    status: this._status(data.reader_status),
                },
                ports: data.port_numbers || "—",
                values: {
                    power: Number(data.reader_power_dbm || 0),
                    interval: Number(data.read_interval_ms || 0),
                    tidStart: Number(data.reader_tid_addr || 0),
                    tidLength: Number(data.reader_tid_len || 0),
                },
            };
        });
    }

    _status(value) {
        const status = String(value || "").toLowerCase();
        if (STATUS_ONLINE.has(status)) {
            return "online";
        }
        if (STATUS_OFFLINE.has(status)) {
            return "offline";
        }
        return "unknown";
    }

    statusLabel(status) {
        return status === "online" ? "Online" : status === "offline" ? "Offline" : "Unknown";
    }

    get tree() {
        const query = this.state.query.trim().toLowerCase();
        const groups = new Map();

        // Parking Lane stores one contextual Server and Controller on the Lane.
        // Keep those nodes visible even before the first Reader is added so the
        // hierarchy can be built directly from the Tree View.
        if (this.mode === "parking_lane") {
            const data = this.props.record?.data || {};
            const serverId = many2oneId(data.edge_server_id);
            const controllerId = many2oneId(data.controller_id);
            if (serverId) {
                const server = {
                    key: `server-${serverId}`,
                    id: serverId,
                    name: many2oneLabel(data.edge_server_id, "Server"),
                    status: this._status(data.edge_server_status),
                    controllers: new Map(),
                };
                groups.set(server.key, server);
                if (controllerId) {
                    const controllerKey = `${server.key}-controller-${controllerId}`;
                    server.controllers.set(controllerKey, {
                        key: controllerKey,
                        id: controllerId,
                        name: many2oneLabel(data.controller_id, "Controller"),
                        status: this._status(data.controller_status),
                        readers: [],
                    });
                }
            }
        }

        for (const entry of this.entries) {
            const haystack = [entry.server.name, entry.controller.name, entry.reader.name, entry.reader.serial]
                .join(" ")
                .toLowerCase();
            if (query && !haystack.includes(query)) {
                continue;
            }
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

        let result = [...groups.values()].map((server) => ({
            ...server,
            controllers: [...server.controllers.values()],
        }));
        if (query) {
            result = result.filter((server) => {
                const serverMatch = server.name.toLowerCase().includes(query);
                const controllers = server.controllers.filter((controller) => {
                    const controllerMatch = controller.name.toLowerCase().includes(query);
                    return controllerMatch || controller.readers.length || serverMatch;
                });
                server.controllers = controllers;
                return serverMatch || controllers.length;
            });
        }
        return result;
    }

    get canAddServer() {
        if (!this.editable) {
            return false;
        }
        if (this.mode === "lane_calibration") {
            // One Lane Calibration owns exactly one contextual Server. Additional
            // Controllers and Readers are added below the existing Server node.
            return this.entries.length === 0;
        }
        return !many2oneId(this.props.record?.data?.edge_server_id);
    }

    canAddController(server) {
        if (!this.editable || !server) {
            return false;
        }
        if (this.mode === "lane_calibration") {
            return true;
        }
        return !many2oneId(this.props.record?.data?.controller_id);
    }

    get selectedEntry() {
        const entries = this.entries;
        return entries.find((entry) => entry.key === this.state.selectedKey) || entries[0] || null;
    }

    get selectedPortList() {
        if (this.mode !== "lane_calibration") {
            return null;
        }
        return this.selectedEntry?.record?.data?.reader_port_ids || null;
    }

    get selectedPorts() {
        const records = this.selectedPortList?.records || [];
        return records
            .map((record, index) => ({
                key: `port-${record.resId || record.id || index}`,
                record,
                portNo: Number(record.data?.port_no || 0),
            }))
            .filter((port) => port.portNo > 0)
            .sort((a, b) => a.portNo - b.portNo);
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

    onSearchInput(ev) {
        this.state.query = ev.target.value || "";
    }

    _openMasterSearch({ title, resModel, domain = [["active", "=", true]], fields, labelField, onSelected }) {
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
                const selection = {
                    id: row.id,
                    name: row[labelField] || row.name || `Record ${row.id}`,
                };
                await onSelected(selection, row);
            },
        });
    }

    _openServerSearch({ title, onSelected }) {
        this._openMasterSearch({
            title,
            resModel: "nsp.edge.server",
            domain: [
                ["active", "=", true],
                ["whitelist_id", "!=", false],
                ["whitelist_id.active", "=", true],
                ["whitelist_id.device_type_code", "=", "SERVER"],
            ],
            fields: ["name", "status"],
            labelField: "name",
            onSelected,
        });
    }

    _openControllerSearch({ title, onSelected }) {
        this._openMasterSearch({
            title,
            resModel: "nsp.controller",
            domain: [
                ["active", "=", true],
                ["whitelist_id", "!=", false],
                ["whitelist_id.active", "=", true],
                ["whitelist_id.device_type_code", "=", "CONTROLLER"],
            ],
            fields: ["controller_name", "status"],
            labelField: "controller_name",
            onSelected,
        });
    }

    _openReaderSearch({ title, onSelected }) {
        this._openMasterSearch({
            title,
            resModel: "nsp.device",
            domain: [
                ["active", "=", true],
                ["whitelist_id", "!=", false],
                ["whitelist_id.active", "=", true],
                ["whitelist_id.device_type_code", "=", "RFID_READER"],
            ],
            fields: ["name", "serial_number", "status"],
            labelField: "name",
            onSelected,
        });
    }

    async addServer() {
        if (!this.canAddServer) {
            return;
        }
        this._openServerSearch({
            title: "Add Server",
            onSelected: async (server) => {
                if (this.mode === "parking_lane") {
                    await this.props.record.update({ edge_server_id: toMany2one(server) });
                    return;
                }
                // Calibration lines store the contextual Server/Controller/Reader
                // association. Build the branch through three native Odoo search
                // dialogs rather than a custom combined selector.
                this._openControllerSearch({
                    title: `Add Controller to ${server.name}`,
                    onSelected: (controller) => this._openReaderSearch({
                        title: `Add Reader to ${controller.name}`,
                        onSelected: (reader) => this._createMapping({ server, controller, reader }),
                    }),
                });
            },
        });
    }

    async editServer(server) {
        if (!server || !this.editable) {
            return;
        }
        this._openServerSearch({
            title: "Edit Server",
            onSelected: async (selection) => {
                if (this.mode === "parking_lane") {
                    await this.props.record.update({ edge_server_id: toMany2one(selection) });
                    return;
                }
                const records = this.entries
                    .filter((entry) => Number(entry.server.id) === Number(server.id))
                    .map((entry) => entry.record);
                await Promise.all(records.map((record) => record.update({ edge_server_id: toMany2one(selection) })));
            },
        });
    }

    deleteServer(server) {
        if (!server || !this.editable) {
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: "Remove Server",
            body: `Remove ${server.name} and its contextual Controller/Reader mappings from this Device Tree?`,
            confirmLabel: "Remove",
            confirm: async () => {
                const records = this.entries
                    .filter((entry) => Number(entry.server.id) === Number(server.id))
                    .map((entry) => entry.record);
                await Promise.all(records.map((record) => this.removeDeviceRecord(record)));
                if (this.mode === "parking_lane") {
                    await this.props.record.update({ edge_server_id: false, controller_id: false });
                }
                this.state.selectedKey = null;
                this.cancelEdit();
            },
        });
    }

    async addController(server) {
        if (!this.canAddController(server)) {
            return;
        }
        this._openControllerSearch({
            title: "Add Controller",
            onSelected: async (controller) => {
                if (this.mode === "parking_lane") {
                    await this.props.record.update({ controller_id: toMany2one(controller) });
                    return;
                }
                this._openReaderSearch({
                    title: `Add Reader to ${controller.name}`,
                    onSelected: (reader) => this._createMapping({ server, controller, reader }),
                });
            },
        });
    }

    async editController(server, controller) {
        if (!controller || !this.editable) {
            return;
        }
        this._openControllerSearch({
            title: "Edit Controller",
            onSelected: async (selection) => {
                if (this.mode === "parking_lane") {
                    await this.props.record.update({ controller_id: toMany2one(selection) });
                    return;
                }
                const records = this.entries
                    .filter((entry) => Number(entry.server.id) === Number(server.id)
                        && Number(entry.controller.id) === Number(controller.id))
                    .map((entry) => entry.record);
                await Promise.all(records.map((record) => record.update({ controller_id: toMany2one(selection) })));
            },
        });
    }

    deleteController(server, controller) {
        if (!controller || !this.editable) {
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: "Remove Controller",
            body: `Remove ${controller.name} and its Reader mappings from this Device Tree?`,
            confirmLabel: "Remove",
            confirm: async () => {
                const records = this.entries
                    .filter((entry) => Number(entry.server.id) === Number(server.id)
                        && Number(entry.controller.id) === Number(controller.id))
                    .map((entry) => entry.record);
                await Promise.all(records.map((record) => this.removeDeviceRecord(record)));
                if (this.mode === "parking_lane") {
                    await this.props.record.update({ controller_id: false });
                }
                this.state.selectedKey = null;
                this.cancelEdit();
            },
        });
    }

    async addReader(server, controller) {
        if (!this.editable || !this.deviceList || !server || !controller) {
            return;
        }
        this._openReaderSearch({
            title: "Add Reader",
            onSelected: (reader) => this._createMapping({ server, controller, reader }),
        });
    }

    async editMapping(entry) {
        if (!entry || !this.editable) {
            return;
        }
        this._openReaderSearch({
            title: "Edit Reader",
            onSelected: (reader) => entry.record.update({ reader_id: toMany2one(reader) }),
        });
    }

    async _createMapping({ server, controller, reader }) {
        const list = this.deviceList;
        if (!list) {
            return;
        }
        if (this.mode === "parking_lane") {
            await this.props.record.update({
                edge_server_id: toMany2one(server),
                controller_id: toMany2one(controller),
            });
        }
        const line = await list.addNewRecord({
            context: list.context || {},
            mode: "edit",
            position: "bottom",
        });
        const values = this.mode === "parking_lane"
            ? { reader_id: toMany2one(reader) }
            : {
                edge_server_id: toMany2one(server),
                controller_id: toMany2one(controller),
                reader_id: toMany2one(reader),
            };
        await line.update(values);
        this.state.selectedKey = this.mode === "parking_lane"
            ? `lane-reader-${line.resId || line.id}`
            : `cal-reader-${line.resId || line.id}`;
    }

    deleteMapping(entry) {
        if (!entry || !this.editable) {
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: "Remove Reader",
            body: `Remove ${entry.reader.name} from this Device Tree?`,
            confirmLabel: "Remove",
            confirm: async () => {
                await this.removeDeviceRecord(entry.record);
                this.state.selectedKey = null;
                this.state.editing = false;
                this.state.draft = {};
                this.state.errors = {};
            },
        });
    }

    async addPort() {
        if (this.mode !== "lane_calibration" || !this.editable) {
            return;
        }
        const list = this.selectedPortList;
        if (!list) {
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
        const portRecord = await list.addNewRecord({
            context: list.context || {},
            mode: "edit",
            position: "bottom",
        });
        await portRecord.update({ port_no: nextPort });
        if (list.leaveEditMode) {
            await list.leaveEditMode();
        }
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
        if (this.selectedPorts.some((item) => item.key !== port.key && item.portNo === value)) {
            this.state.portError = `P${value} is already configured for this Reader.`;
            return;
        }
        await port.record.update({ port_no: value });
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
                await this.removePortRecord(port.record);
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

    async saveConfiguration() {
        const entry = this.selectedEntry;
        if (!entry || !this._validateDraft() || this.state.saving) {
            return;
        }
        this.state.saving = true;
        this.state.configState = "Saving...";
        const draft = this.state.draft;
        const values = this.mode === "parking_lane"
            ? {
                power_dbm: Number(draft.power),
                read_interval_ms: Number(draft.interval),
                tid_start_address: Number(draft.tidStart),
                tid_length: Number(draft.tidLength),
            }
            : {
                reader_power_dbm: Number(draft.power),
                read_interval_ms: Number(draft.interval),
                reader_tid_addr: Number(draft.tidStart),
                reader_tid_len: Number(draft.tidLength),
            };
        try {
            await entry.record.update(values);
            this.state.editing = false;
            this.state.configState = "Saved";
            this.state.draft = {};
            this.state.errors = {};
            this.notification.add("Reader configuration updated in the current form.", {
                title: "Configuration updated",
                type: "success",
            });
        } catch (error) {
            this.state.configState = "Save failed";
            this.notification.add("Unable to update Reader configuration.", {
                title: "Configuration error",
                type: "danger",
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
