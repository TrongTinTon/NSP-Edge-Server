/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const SERIES_COLORS = ["#2563eb", "#f59e0b", "#06b6d4", "#16a34a", "#7c3aed", "#db2777", "#64748b"];

export class NspMeasurementLive extends Component {
    static template = "nsp_gatekeeper.MeasurementLive";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.sessionId = this.resolveSessionId();
        this.timer = null;
        this.loading = false;
        this.state = useState({
            measurementCode: "",
            revision: 1,
            status: "",
            controllerCode: "",
            controllerName: "",
            edgeServerCode: "",
            edgeStatus: "",
            targetTag: {},
            readers: [],
            readerCount: 0,
            startedAt: null,
            endedAt: null,
            note: "",
            steps: [],
            rawEventCount: 0,
            detectionCount: 0,
            uniqueAntennas: 0,
            uniqueReaders: 0,
            serverTime: null,
            activeTab: "timeline",
            error: "",
            hasLoaded: false,
        });
        onMounted(() => {
            this.refresh();
            this.timer = setInterval(() => this.refresh(), 1000);
        });
        onWillUnmount(() => {
            if (this.timer) {
                clearInterval(this.timer);
            }
        });
    }

    normalizeSessionId(value) {
        const id = Number(value);
        return Number.isInteger(id) && id > 0 ? id : null;
    }

    resolveSessionId() {
        const action = this.props.action || {};
        const context = action.context || {};
        const params = action.params || {};
        const candidates = [
            params.session_id,
            context.active_id,
            context.default_session_id,
            action.res_id,
            this.props.session_id,
            this.props.active_id,
        ];
        for (const value of candidates) {
            const id = this.normalizeSessionId(value);
            if (id) {
                return id;
            }
        }
        return null;
    }

    requireSessionId() {
        const id = this.normalizeSessionId(this.sessionId);
        if (!id) {
            this.notification.add(
                "Measurement Session context is missing. Return to Measurement Sessions and open Live Measurement again.",
                {type: "warning"}
            );
            return null;
        }
        return id;
    }

    _mergeReaders(incoming, resetDraft) {
        const existing = new Map(
            (this.state.readers || []).map((reader) => [Number(reader.reader_line_id), reader])
        );
        return (incoming || []).map((raw) => {
            const serverPower = Number(raw.measurement_power_dbm ?? 0);
            const previous = existing.get(Number(raw.reader_line_id));
            const draft = resetDraft || !previous
                ? serverPower
                : Number(previous.powerDraft);
            return {
                ...raw,
                serverMeasurementPower: serverPower,
                powerDraft: Number.isFinite(draft) ? draft : serverPower,
            };
        });
    }

    async refresh() {
        if (!this.sessionId || this.loading) {
            return;
        }
        this.loading = true;
        try {
            const data = await this.orm.call(
                "nsp.measurement.session",
                "get_live_snapshot",
                [this.sessionId, 0, 500]
            );
            if (!data?.found) {
                this.state.error = "Measurement Session was not found.";
                return;
            }
            const returnedSessionId = this.normalizeSessionId(data.session_id);
            if (returnedSessionId) {
                this.sessionId = returnedSessionId;
            }
            const previousRevision = this.state.revision;
            const incomingRevision = data.revision || 1;
            const resetDraft = !this.state.hasLoaded || previousRevision !== incomingRevision;
            this.state.measurementCode = data.measurement_code || "";
            this.state.revision = incomingRevision;
            this.state.status = data.status || "";
            this.state.controllerCode = data.controller_code || "";
            this.state.controllerName = data.controller_name || "";
            this.state.edgeServerCode = data.edge_server_code || "";
            this.state.edgeStatus = data.edge_status || "";
            this.state.targetTag = data.target_tag || {};
            this.state.readers = this._mergeReaders(data.readers || [], resetDraft);
            this.state.readerCount = data.reader_count || this.state.readers.length;
            this.state.hasLoaded = true;
            this.state.startedAt = data.started_at || null;
            this.state.endedAt = data.ended_at || null;
            this.state.note = data.note || "";
            this.state.steps = data.steps || [];
            this.state.rawEventCount = data.raw_event_count || 0;
            this.state.detectionCount = data.detection_count || 0;
            this.state.uniqueAntennas = data.unique_antennas || 0;
            this.state.uniqueReaders = data.unique_readers || 0;
            this.state.serverTime = data.server_time || null;
            this.state.error = "";
        } catch (error) {
            this.state.error = error?.message || "Unable to load Live Measurement data.";
        } finally {
            this.loading = false;
        }
    }

    statusLabel() {
        const labels = {
            draft: "Draft",
            ready: "Ready",
            running: "In Progress",
            completed: "Completed",
            applied: "Applied",
            failed: "Failed",
            cancelled: "Cancelled",
        };
        return labels[this.state.status] || this.state.status || "Loading";
    }

    statusClass() {
        return `nsp-ml__status nsp-ml__status--${this.state.status || "loading"}`;
    }

    targetDescription() {
        const tag = this.state.targetTag || {};
        const type = tag.card_type === "vehicle_card" ? "Vehicle Card" : tag.card_type === "user_card" ? "User Card" : "RFID Tag";
        return tag.assigned_to ? `${type} • ${tag.assigned_to}` : type;
    }

    setTimelineTab() {
        this.state.activeTab = "timeline";
    }

    setReaderTab() {
        this.state.activeTab = "reader";
    }

    setSessionTab() {
        this.state.activeTab = "session";
    }

    setNotesTab() {
        this.state.activeTab = "notes";
    }

    tabClass(tab) {
        return this.state.activeTab === tab ? "nsp-ml__tab nsp-ml__tab--active" : "nsp-ml__tab";
    }

    parseDate(value) {
        if (!value) {
            return null;
        }
        const text = String(value).trim();
        const normalized = text.includes("T") ? text : text.replace(" ", "T") + "Z";
        const parsed = new Date(normalized);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    formatTime(value) {
        const date = this.parseDate(value);
        if (!date) {
            return "-";
        }
        const h = String(date.getHours()).padStart(2, "0");
        const m = String(date.getMinutes()).padStart(2, "0");
        const s = String(date.getSeconds()).padStart(2, "0");
        const ms = String(date.getMilliseconds()).padStart(3, "0");
        return `${h}:${m}:${s}.${ms}`;
    }

    formatDateTime(value) {
        const date = this.parseDate(value);
        if (!date) {
            return "-";
        }
        return `${String(date.getDate()).padStart(2, "0")}/${String(date.getMonth() + 1).padStart(2, "0")}/${date.getFullYear()} ${this.formatTime(value)}`;
    }

    formatRssi(value) {
        return value === false || value === null || value === undefined
            ? "-"
            : `${Number(value).toFixed(0)} dBm`;
    }

    stepDuration(step) {
        const first = this.parseDate(step?.first_seen_at);
        const last = this.parseDate(step?.last_seen_at);
        if (!first || !last) {
            return "-";
        }
        return `${Math.max(0, last.getTime() - first.getTime())} ms`;
    }

    measurementDuration() {
        const first = this.firstStep();
        const last = this.lastStep();
        const start = this.parseDate(first?.first_seen_at);
        const end = this.parseDate(last?.last_seen_at);
        if (!start || !end) {
            return "00:00:00";
        }
        const total = Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
        const h = String(Math.floor(total / 3600)).padStart(2, "0");
        const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
        const s = String(total % 60).padStart(2, "0");
        return `${h}:${m}:${s}`;
    }

    firstStep() {
        return this.state.steps[0] || null;
    }

    lastStep() {
        return this.state.steps.length ? this.state.steps[this.state.steps.length - 1] : null;
    }

    firstDetectedAt() {
        return this.firstStep()?.first_seen_at || null;
    }

    lastDetectedAt() {
        return this.lastStep()?.last_seen_at || null;
    }

    antennaList(reader) {
        const values = reader?.antennas || [];
        return values.length ? values.map((value) => `ANT${value}`).join(", ") : "-";
    }

    readerDetectionCount(reader) {
        const serial = String(reader?.serial_number || "").toUpperCase();
        return (this.state.steps || []).filter(
            (step) => String(step.serial_number || "").toUpperCase() === serial
        ).length;
    }

    sequenceClass(sequenceNo) {
        return `nsp-ml__sequence nsp-ml__sequence--${((Number(sequenceNo) - 1) % 6) + 1}`;
    }

    edgeDotClass() {
        return this.state.edgeStatus === "online" ? "nsp-ml__edge-dot nsp-ml__edge-dot--online" : "nsp-ml__edge-dot";
    }

    canAdjustPower() {
        return ["running", "completed", "failed"].includes(this.state.status);
    }

    canMeasureAgain() {
        return ["running", "completed", "failed"].includes(this.state.status) && this.state.readers.length > 0;
    }

    readerPowerMatches(reader) {
        const draftPower = Number(reader?.powerDraft);
        const serverPower = Number(reader?.serverMeasurementPower);
        return Number.isFinite(draftPower)
            && Number.isFinite(serverPower)
            && draftPower === serverPower;
    }

    allReaderPowersMatch() {
        return this.state.readers.length > 0 && this.state.readers.every((reader) => this.readerPowerMatches(reader));
    }

    canApply() {
        return this.state.status === "completed" && this.allReaderPowersMatch();
    }

    canStop() {
        return ["ready", "running"].includes(this.state.status);
    }

    _readerFromEvent(event) {
        const id = Number(event?.currentTarget?.dataset?.readerLineId || 0);
        return this.state.readers.find((reader) => Number(reader.reader_line_id) === id) || null;
    }

    decreaseReaderPower(event) {
        if (!this.canAdjustPower()) {
            return;
        }
        const reader = this._readerFromEvent(event);
        if (reader) {
            reader.powerDraft = Math.max(0, Number(reader.powerDraft || 0) - 1);
        }
    }

    increaseReaderPower(event) {
        if (!this.canAdjustPower()) {
            return;
        }
        const reader = this._readerFromEvent(event);
        if (reader) {
            reader.powerDraft = Math.min(40, Number(reader.powerDraft || 0) + 1);
        }
    }

    focusPower() {
        document.querySelector(".nsp-ml-reader-power")?.scrollIntoView({behavior: "smooth", block: "center"});
    }

    async measureAgain() {
        if (!this.canMeasureAgain()) {
            return;
        }
        const powers = this.state.readers.map((reader) => ({
            reader_line_id: Number(reader.reader_line_id),
            power_dbm: Number(reader.powerDraft),
        }));
        try {
            await this.orm.call(
                "nsp.measurement.session",
                "action_measure_again",
                [[this.sessionId], powers]
            );
            this.notification.add(
                `Revision ${Number(this.state.revision) + 1} released for ${powers.length} Reader(s).`,
                {type: "success"}
            );
            await this.refresh();
        } catch (error) {
            this.notification.add(error?.message || "Unable to start a new Measurement revision.", {type: "danger"});
        }
    }

    async completeMeasurement() {
        try {
            await this.orm.call("nsp.measurement.session", "action_complete", [[this.sessionId]]);
            this.notification.add("Measurement completed.", {type: "success"});
            await this.refresh();
        } catch (error) {
            this.notification.add(error?.message || "Unable to complete Measurement.", {type: "danger"});
        }
    }

    async stopMeasurement() {
        try {
            await this.orm.call("nsp.measurement.session", "action_cancel", [[this.sessionId]]);
            this.notification.add("Measurement cancelled.", {type: "warning"});
            await this.refresh();
        } catch (error) {
            this.notification.add(error?.message || "Unable to stop Measurement.", {type: "danger"});
        }
    }

    async applyToOperation() {
        if (!this.canApply()) {
            return;
        }
        try {
            await this.orm.call("nsp.measurement.session", "action_apply_to_operation", [[this.sessionId]]);
            this.notification.add(
                `${this.state.readers.length} Reader operation profile(s) updated.`,
                {type: "success"}
            );
            await this.refresh();
        } catch (error) {
            this.notification.add(error?.message || "Unable to apply Measurement to operation.", {type: "danger"});
        }
    }

    goToSession() {
        const sessionId = this.requireSessionId();
        if (!sessionId) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "nsp.measurement.session",
            res_id: sessionId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async openRevisionHistory() {
        const sessionId = this.requireSessionId();
        if (!sessionId) {
            return;
        }
        try {
            const action = await this.orm.call(
                "nsp.measurement.session",
                "action_view_events",
                [[sessionId]]
            );
            await this.action.doAction(action);
        } catch (error) {
            this.notification.add(
                error?.message || "Unable to open Measurement Revision History.",
                {type: "danger"}
            );
        }
    }

    exportCsv() {
        const rows = [["#", "Detected At", "Controller", "Reader", "Antenna", "Power dBm", "Peak RSSI dBm", "Reads", "Duration ms"]];
        for (const step of this.state.steps) {
            const first = this.parseDate(step.first_seen_at);
            const last = this.parseDate(step.last_seen_at);
            rows.push([
                step.sequence_no,
                step.first_seen_at || "",
                step.controller_code || "",
                step.reader_name || "",
                step.antenna_no,
                step.power_dbm,
                step.peak_rssi_dbm ?? "",
                step.read_count,
                first && last ? Math.max(0, last.getTime() - first.getTime()) : "",
            ]);
        }
        const csv = rows.map((row) => row.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")).join("\n");
        const blob = new Blob([csv], {type: "text/csv;charset=utf-8"});
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `${this.state.measurementCode || "measurement"}-R${this.state.revision}.csv`;
        anchor.click();
        URL.revokeObjectURL(url);
    }

    chartGrid() {
        return [-40, -60, -80, -100].map((value) => ({value, y: this.rssiY(value)}));
    }

    rssiY(value) {
        const min = -100;
        const max = -30;
        const bounded = Math.max(min, Math.min(max, Number(value)));
        return 25 + ((max - bounded) / (max - min)) * 160;
    }

    chartSeries() {
        const steps = this.state.steps || [];
        if (!steps.length) {
            return [];
        }
        const groups = new Map();
        steps.forEach((step, index) => {
            if (step.peak_rssi_dbm === null || step.peak_rssi_dbm === undefined) {
                return;
            }
            const key = `${step.serial_number}-ANT${step.antenna_no}`;
            if (!groups.has(key)) {
                groups.set(key, []);
            }
            const x = steps.length === 1 ? 465 : 70 + (index / (steps.length - 1)) * 790;
            groups.get(key).push({x, y: this.rssiY(step.peak_rssi_dbm), key: `${key}-${index}`});
        });
        return [...groups.entries()].map(([key, points], index) => ({
            key,
            label: key,
            color: SERIES_COLORS[index % SERIES_COLORS.length],
            points: points.map((point) => `${point.x},${point.y}`).join(" "),
            circles: points,
        }));
    }
}

registry.category("actions").add("nsp_measurement_live", NspMeasurementLive);
