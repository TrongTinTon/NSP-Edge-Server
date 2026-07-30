/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class NspMeasurementLive extends Component {
    static template = "nsp_business_gatekeeper.MeasurementLive";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.sessionId = this.resolveSessionId();
        this.timer = null;
        this.loading = false;
        this.state = useState({
            measurementCode: "",
            deploymentRole: "",
            revision: 1,
            status: "",
            controllers: [],
            controllerCount: 0,
            edgeServerCodes: [],
            targetPairs: [],
            targetCount: 0,
            targetTagCount: 0,
            detectedTargetCount: 0,
            coveragePercent: 0,
            readers: [],
            readerCount: 0,
            startedAt: null,
            endedAt: null,
            appliedAt: null,
            plannedStartAt: null,
            plannedEndAt: null,
            note: "",
            steps: [],
            rawEventCount: 0,
            detectionCount: 0,
            uniqueAntennas: 0,
            uniqueReaders: 0,
            uniqueControllers: 0,
            serverTime: null,
            activeTab: "timeline",
            error: "",
            hasLoaded: false,
            readerConfigCollapsed: false,
        });
        onMounted(() => {
            this.refresh();
            this.timer = window.setInterval(() => this.refresh(), 1000);
        });
        onWillUnmount(() => {
            if (this.timer) {
                window.clearInterval(this.timer);
            }
        });
    }

    normalizeSessionId(value) {
        const id = Number(value);
        return Number.isInteger(id) && id > 0 ? id : null;
    }

    resolveSessionId() {
        const candidates = [
            this.props.record?.resId,
            this.props.record?.data?.id,
            this.props.record?.data?.res_id,
        ];
        for (const value of candidates) {
            const id = this.normalizeSessionId(value);
            if (id) {
                return id;
            }
        }
        return null;
    }

    _mergeReaders(incoming, resetDraft) {
        const existing = new Map(
            (this.state.readers || []).map((reader) => [Number(reader.reader_line_id), reader])
        );
        return (incoming || []).map((raw) => {
            const serverPower = Number(raw.reader_power_dbm ?? 0);
            const serverInterval = Number(raw.read_interval_ms ?? 200);
            const previous = existing.get(Number(raw.reader_line_id));
            const powerDraft = resetDraft || !previous ? serverPower : Number(previous.powerDraft);
            const intervalDraft = resetDraft || !previous ? serverInterval : Number(previous.intervalDraft);
            return {
                ...raw,
                serverReaderPower: serverPower,
                serverReadInterval: serverInterval,
                powerDraft: Number.isFinite(powerDraft) ? powerDraft : serverPower,
                intervalDraft: Number.isFinite(intervalDraft) ? intervalDraft : serverInterval,
                applying: false,
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
                [this.sessionId, 0, 5000]
            );
            if (!data?.found) {
                this.state.error = "Measurement Session was not found.";
                return;
            }
            const returnedSessionId = this.normalizeSessionId(data.session_id);
            if (returnedSessionId) {
                this.sessionId = returnedSessionId;
            }
            const incomingRevision = Number(data.revision || 1);
            const resetDraft = !this.state.hasLoaded || Number(this.state.revision) !== incomingRevision;
            this.state.measurementCode = data.measurement_code || "";
            this.state.deploymentRole = data.deployment_role || "";
            this.state.revision = incomingRevision;
            this.state.status = data.status || "";
            this.state.controllers = data.controllers || [];
            this.state.controllerCount = Number(data.controller_count || this.state.controllers.length);
            this.state.edgeServerCodes = data.edge_server_codes || [];
            this.state.targetPairs = data.target_pairs || [];
            this.state.targetCount = Number(data.target_count || this.state.targetPairs.length);
            this.state.targetTagCount = Number(data.target_tag_count || this.state.targetCount * 2);
            this.state.detectedTargetCount = Number(data.detected_target_count || 0);
            this.state.coveragePercent = Number(data.coverage_percent || 0);
            this.state.readers = this._mergeReaders(data.readers || [], resetDraft);
            this.state.readerCount = Number(data.reader_count || this.state.readers.length);
            this.state.startedAt = data.started_at || null;
            this.state.endedAt = data.ended_at || null;
            this.state.appliedAt = data.applied_at || null;
            this.state.plannedStartAt = data.planned_start_at || null;
            this.state.plannedEndAt = data.planned_end_at || null;
            this.state.note = data.note || "";
            this.state.steps = data.steps || [];
            this.state.rawEventCount = Number(data.raw_event_count || 0);
            this.state.detectionCount = Number(data.detection_count || 0);
            this.state.uniqueAntennas = Number(data.unique_antennas || 0);
            this.state.uniqueReaders = Number(data.unique_readers || 0);
            this.state.uniqueControllers = Number(data.unique_controllers || 0);
            this.state.serverTime = data.server_time || null;
            this.state.hasLoaded = true;
            this.state.error = "";
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || "Unable to load Live Measurement data.";
        } finally {
            this.loading = false;
        }
    }

    statusLabel() {
        const labels = {
            draft: "Draft",
            ready: "Released",
            running: "In Progress",
            completed: "Completed",
            applied: "Applied",
            failed: "Failed",
            cancelled: "Cancelled",
        };
        return labels[this.state.status] || this.state.status || "Loading";
    }

    detectionStatusClass(detected) {
        return detected
            ? "nsp-ml__coverage-badge nsp-ml__coverage-badge--detected"
            : "nsp-ml__coverage-badge nsp-ml__coverage-badge--missing";
    }

    detectionStatusLabel(detected) {
        return detected ? "Detected" : "Not detected";
    }

    pairStatusLabel(pair) {
        if (pair?.detected) {
            return "Complete";
        }
        if (pair?.detected_tag_count) {
            return "Partial";
        }
        return "Not detected";
    }

    coverageWidth() {
        const value = Number(this.state.coveragePercent || 0);
        return `${Math.max(0, Math.min(100, value))}%`;
    }

    setTimelineTab() {
        this.state.activeTab = "timeline";
    }

    setCoverageTab() {
        this.state.activeTab = "coverage";
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
        const normalized = text.includes("T") ? text : `${text.replace(" ", "T")}Z`;
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

    antennaList(reader) {
        const values = reader?.antennas || [];
        return values.length ? values.map((value) => `ANT${value}`).join(", ") : "-";
    }

    controllerCodes() {
        const codes = (this.state.controllers || []).map((controller) => controller.code).filter(Boolean);
        return codes.length ? codes.join(", ") : "-";
    }

    edgeServerCodes() {
        return this.state.edgeServerCodes.length ? this.state.edgeServerCodes.join(", ") : "-";
    }

    readerDetectionCount(reader) {
        const serial = String(reader?.serial_number || "").toUpperCase();
        return (this.state.steps || []).filter(
            (step) => String(step.serial_number || "").toUpperCase() === serial
        ).reduce((total, step) => total + Number(step.read_count || 0), 0);
    }

    canEditReaderSettings() {
        return ["running", "completed", "failed"].includes(this.state.status);
    }

    readerSettingsMatch(reader) {
        const draftPower = Number(reader?.powerDraft);
        const serverPower = Number(reader?.serverReaderPower);
        const draftInterval = Number(reader?.intervalDraft);
        const serverInterval = Number(reader?.serverReadInterval);
        return Number.isFinite(draftPower)
            && Number.isFinite(serverPower)
            && Number.isFinite(draftInterval)
            && Number.isFinite(serverInterval)
            && draftPower === serverPower
            && draftInterval === serverInterval;
    }

    readerApplyDisabled(reader) {
        return !this.canEditReaderSettings() || this.readerSettingsMatch(reader) || Boolean(reader?.applying);
    }

    _readerFromEvent(event) {
        const id = Number(event?.currentTarget?.dataset?.readerLineId || 0);
        return this.state.readers.find((reader) => Number(reader.reader_line_id) === id) || null;
    }

    toggleReaderConfiguration() {
        this.state.readerConfigCollapsed = !this.state.readerConfigCollapsed;
    }

    updateReaderPower(event) {
        if (!this.canEditReaderSettings()) {
            return;
        }
        const reader = this._readerFromEvent(event);
        if (reader) {
            reader.powerDraft = Math.max(0, Math.min(40, Number(event.target.value || 0)));
        }
    }

    updateReadInterval(event) {
        if (!this.canEditReaderSettings()) {
            return;
        }
        const reader = this._readerFromEvent(event);
        if (reader) {
            reader.intervalDraft = Math.max(1, Math.min(60000, Number(event.target.value || 200)));
        }
    }

    async applyReaderSettings(event) {
        const reader = this._readerFromEvent(event);
        if (!reader || this.readerApplyDisabled(reader)) {
            return;
        }
        reader.applying = true;
        try {
            await this.orm.call(
                "nsp.measurement.session",
                "action_apply_reader_settings",
                [[this.sessionId], Number(reader.reader_line_id), Number(reader.powerDraft), Number(reader.intervalDraft)]
            );
            this.notification.add(
                `${reader.name || reader.serial_number}: settings released as a new revision.`,
                { type: "success" }
            );
            await this.refresh();
        } catch (error) {
            reader.applying = false;
            this.notification.add(
                error?.data?.message || error?.message || "Unable to apply Reader settings.",
                { type: "danger" }
            );
        }
    }

    exportCsv() {
        const rows = [[
            "#", "First Detected", "RFID Tag", "Assigned To", "Controller",
            "Reader", "Antenna", "Reads", "Last Detected", "Duration ms",
        ]];
        for (const step of this.state.steps) {
            const first = this.parseDate(step.first_seen_at);
            const last = this.parseDate(step.last_seen_at);
            rows.push([
                step.sequence_no,
                step.first_seen_at || "",
                step.tid || "",
                step.assigned_to || "",
                step.controller_code || "",
                step.reader_name || step.serial_number || "",
                step.antenna_no,
                step.read_count,
                step.last_seen_at || "",
                first && last ? Math.max(0, last.getTime() - first.getTime()) : "",
            ]);
        }
        const csv = rows
            .map((row) => row.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(","))
            .join("\n");
        const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `${this.state.measurementCode || "measurement"}-R${this.state.revision}.csv`;
        anchor.click();
        URL.revokeObjectURL(url);
    }
}

export const nspMeasurementLiveField = {
    component: NspMeasurementLive,
    supportedTypes: ["boolean"],
};

registry.category("fields").add("nsp_business_measurement_live_dashboard", nspMeasurementLiveField);
