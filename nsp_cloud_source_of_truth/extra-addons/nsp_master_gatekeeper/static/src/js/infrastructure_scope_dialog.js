/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

export class NspInfrastructureScopeDialog extends Component {
    static template = "nsp_master_gatekeeper.InfrastructureScopeDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        sessionId: Number,
        onChanged: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.timer = null;
        this.loading = false;
        this.initialized = false;
        this.state = useState({
            data: {
                found: false,
                summary: {},
                edges: [],
                readers: [],
                warnings: [],
            },
            tab: "live",
            selectedReaderLineId: 0,
            expandedEdges: [],
            expandedControllers: [],
            expandedReaders: [],
            error: "",
        });

        onWillStart(() => this.refresh());
        onMounted(() => {
            this.timer = window.setInterval(() => this.refresh(), 2000);
        });
        onWillUnmount(() => {
            if (this.timer) {
                window.clearInterval(this.timer);
            }
        });
    }

    async refresh() {
        if (this.loading) {
            return;
        }
        this.loading = true;
        try {
            const data = await this.orm.call(
                "nsp.measurement.session",
                "get_infrastructure_scope_snapshot",
                [this.props.sessionId]
            );
            if (!data?.found) {
                this.state.error = "Lane Calibration was not found.";
                return;
            }
            this.state.data = data;
            this.state.error = "";
            this.syncUiState(data);
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || "Unable to load Infrastructure Scope.";
        } finally {
            this.loading = false;
        }
    }

    syncUiState(data) {
        const edgeIds = (data.edges || []).map((item) => Number(item.id));
        const controllerIds = (data.edges || []).flatMap((edge) =>
            (edge.controllers || []).map((item) => Number(item.id))
        );
        const readerIds = (data.readers || []).map((item) => Number(item.reader_line_id));

        if (!this.initialized) {
            this.state.expandedEdges = edgeIds;
            this.state.expandedControllers = controllerIds;
            this.state.expandedReaders = readerIds;
            this.state.selectedReaderLineId = readerIds[0] || 0;
            this.initialized = true;
            return;
        }
        this.state.expandedEdges = this.state.expandedEdges.filter((id) => edgeIds.includes(id));
        this.state.expandedControllers = this.state.expandedControllers.filter((id) => controllerIds.includes(id));
        this.state.expandedReaders = this.state.expandedReaders.filter((id) => readerIds.includes(id));
        if (!readerIds.includes(Number(this.state.selectedReaderLineId))) {
            this.state.selectedReaderLineId = readerIds[0] || 0;
        }
    }

    async callSession(method, args = []) {
        try {
            return await this.orm.call(
                "nsp.measurement.session",
                method,
                [[this.props.sessionId], ...args]
            );
        } catch (error) {
            this.notification.add(
                error?.data?.message || error?.message || "The operation could not be completed.",
                { type: "danger" }
            );
            return false;
        }
    }

    setTab(tab) {
        this.state.tab = tab;
    }

    toggleExpanded(bucket, id) {
        const normalized = Number(id);
        const values = this.state[bucket];
        const index = values.indexOf(normalized);
        if (index >= 0) {
            values.splice(index, 1);
        } else {
            values.push(normalized);
        }
    }

    isExpanded(bucket, id) {
        return this.state[bucket].includes(Number(id));
    }

    selectReader(reader) {
        this.state.selectedReaderLineId = Number(reader.reader_line_id || 0);
    }

    selectedReader() {
        const selectedId = Number(this.state.selectedReaderLineId || 0);
        return (this.state.data.readers || []).find(
            (reader) => Number(reader.reader_line_id) === selectedId
        ) || null;
    }

    isReaderSelected(reader) {
        return Number(this.state.selectedReaderLineId || 0) === Number(reader.reader_line_id || 0);
    }

    configuredPortsLabel(reader) {
        const ports = (reader.ports || [])
            .filter((port) => port.configured)
            .map((port) => `P${port.port_no}`);
        return ports.join(", ") || "—";
    }

    async openReader(readerLineId = 0) {
        const action = await this.callSession(
            "action_open_infrastructure_reader",
            [Number(readerLineId || 0)]
        );
        if (!action?.type) {
            return;
        }
        await this.action.doAction(action, {
            onClose: async () => {
                await this.refresh();
                if (this.props.onChanged) {
                    await this.props.onChanged();
                }
            },
        });
    }

    async removeReader(reader) {
        if (!window.confirm(`Remove Reader Assembly ${reader.name || reader.serial_number || ""}?`)) {
            return;
        }
        const result = await this.callSession(
            "action_remove_infrastructure_reader",
            [Number(reader.reader_line_id || 0)]
        );
        if (result !== false) {
            this.notification.add("Reader Assembly removed.", { type: "success" });
            await this.refresh();
            if (this.props.onChanged) {
                await this.props.onChanged();
            }
        }
    }

    statusLabel(status) {
        const labels = {
            online: "Online",
            offline: "Offline",
            error: "Error",
            block: "Blocked",
            revoked: "Revoked",
            degraded: "Degraded",
        };
        return labels[status] || "Unknown";
    }

    statusTone(status) {
        if (status === "online") {
            return "success";
        }
        if (["error", "block", "revoked"].includes(status)) {
            return "danger";
        }
        if (status === "degraded") {
            return "warning";
        }
        return "muted";
    }

    activityLabel(status) {
        const labels = {
            active: "Active",
            silent: "Silent",
            connected: "Connected",
            degraded: "Degraded",
            offline: "Offline",
            historical: "Historical",
            unknown: "Unknown",
        };
        return labels[status] || "Unknown";
    }

    activityTone(status) {
        if (status === "active") {
            return "success";
        }
        if (["silent", "degraded"].includes(status)) {
            return "warning";
        }
        if (status === "offline") {
            return "danger";
        }
        if (status === "connected") {
            return "info";
        }
        return "muted";
    }

    portActivityLabel(port) {
        if (!port.configured && port.detection_count > 0) {
            return `Observed ${port.detection_count} events outside scope`;
        }
        if (port.activity === "active") {
            return `Detected ${port.detection_count} events`;
        }
        if (port.activity === "historical") {
            return `${port.detection_count} historical events`;
        }
        if (port.activity === "silent") {
            return "No recent detection";
        }
        return "No detection";
    }

    summaryStatus(online, total) {
        if (!total) {
            return "muted";
        }
        if (online === total) {
            return "success";
        }
        if (online > 0) {
            return "warning";
        }
        return "danger";
    }

    summaryText(online, total) {
        if (!total) {
            return "Not configured";
        }
        return total === 1
            ? (online === 1 ? "Online" : "Offline")
            : `${online} / ${total} online`;
    }

    readerSummaryText() {
        const summary = this.state.data.summary || {};
        if (!summary.reader_total) {
            return "Not configured";
        }
        return `${summary.reader_connected || 0} / ${summary.reader_total || 0} connected`;
    }

    formatDateTime(value) {
        const date = this.parseDate(value);
        if (!date) {
            return "—";
        }
        return new Intl.DateTimeFormat(undefined, {
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        }).format(date);
    }

    formatRelative(value) {
        const date = this.parseDate(value);
        if (!date) {
            return "Never";
        }
        const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
        if (seconds < 5) {
            return "Just now";
        }
        if (seconds < 60) {
            return `${seconds} seconds ago`;
        }
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) {
            return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
        }
        const hours = Math.floor(minutes / 60);
        if (hours < 24) {
            return `${hours} hour${hours === 1 ? "" : "s"} ago`;
        }
        const days = Math.floor(hours / 24);
        return `${days} day${days === 1 ? "" : "s"} ago`;
    }

    parseDate(value) {
        if (!value) {
            return null;
        }
        const raw = String(value);
        const normalized = raw.includes("T") ? raw : `${raw.replace(" ", "T")}Z`;
        const date = new Date(normalized);
        return Number.isNaN(date.getTime()) ? null : date;
    }

    warningIcon(severity) {
        if (severity === "danger") {
            return "fa-times-circle";
        }
        if (severity === "info") {
            return "fa-info-circle";
        }
        return "fa-exclamation-triangle";
    }
}
