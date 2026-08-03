/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class NspCalibrationWorkspace extends Component {
    static template = "nsp_master_gatekeeper.CalibrationWorkspace";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.sessionId = this.resolveSessionId();
        this.timer = null;
        this.loading = false;
        this.state = useState({
            activeTab: "reference",
            selectedRunId: false,
            data: { found: false, passes: [], validation_runs: [] },
            error: "",
        });
        onMounted(() => {
            this.refresh();
            this.timer = window.setInterval(() => this.refresh(), 2000);
        });
        onWillUnmount(() => {
            if (this.timer) {
                window.clearInterval(this.timer);
            }
        });
    }

    resolveSessionId() {
        const candidates = [
            this.props.record?.resId,
            this.props.record?.data?.id,
            this.props.record?.data?.res_id,
        ];
        for (const value of candidates) {
            const id = Number(value);
            if (Number.isInteger(id) && id > 0) {
                return id;
            }
        }
        return null;
    }

    async refresh() {
        if (!this.sessionId || this.loading) {
            return;
        }
        this.loading = true;
        try {
            const data = await this.orm.call(
                "nsp.measurement.session",
                "get_calibration_workspace",
                [this.sessionId, this.state.selectedRunId || false]
            );
            if (!data?.found) {
                this.state.error = "Lane Calibration was not found.";
                return;
            }
            this.state.data = data;
            if (!this.state.selectedRunId && data.active_validation_run?.id) {
                this.state.selectedRunId = data.active_validation_run.id;
            }
            this.state.error = "";
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || "Unable to load Lane Calibration.";
        } finally {
            this.loading = false;
        }
    }

    async callModel(model, method, ids = [], args = []) {
        try {
            const result = await this.orm.call(model, method, [ids, ...args]);
            await this.refresh();
            return result;
        } catch (error) {
            this.notification.add(
                error?.data?.message || error?.message || "The operation could not be completed.",
                { type: "danger" }
            );
            return false;
        }
    }

    async callSession(method, args = []) {
        return this.callModel("nsp.measurement.session", method, [this.sessionId], args);
    }

    async startReferencePass() {
        await this.callSession("action_start_reference_pass");
    }

    async stopReferencePass() {
        await this.callSession("action_stop_reference_pass");
    }

    async acceptPass(passId) {
        await this.callModel("nsp.measurement.pass", "action_accept", [passId]);
    }

    async rejectPass(passId) {
        await this.callModel("nsp.measurement.pass", "action_reject", [passId]);
    }

    async buildResult() {
        const action = await this.callSession("action_build_calibration_result");
        if (action?.type) {
            await this.action.doAction(action);
        }
    }

    async submitResultValidation(resultId) {
        await this.callModel("nsp.measurement.result", "action_submit_validation", [resultId]);
    }

    async publishResult(resultId) {
        await this.callModel("nsp.measurement.result", "action_accept", [resultId]);
    }

    async newValidationRun() {
        const action = await this.callSession("action_new_validation_run");
        if (action?.type) {
            await this.action.doAction(action);
        }
    }

    async openValidationRun(runId) {
        const action = await this.callModel("nsp.measurement.validation.run", "action_open_form", [runId]);
        if (action?.type) {
            await this.action.doAction(action);
        }
    }

    async startValidation(runId) {
        await this.callModel("nsp.measurement.validation.run", "action_start", [runId]);
    }

    async stopValidation(runId) {
        await this.callModel("nsp.measurement.validation.run", "action_stop_and_analyse", [runId]);
    }

    async retestFailed(runId) {
        const action = await this.callModel("nsp.measurement.validation.run", "action_retest_failed", [runId]);
        if (action?.type) {
            await this.action.doAction(action);
        }
    }

    async retestSelected(runId) {
        const action = await this.callModel("nsp.measurement.validation.run", "action_retest_selected", [runId]);
        if (action?.type) {
            await this.action.doAction(action);
        }
    }

    async toggleRetestVehicle(vehicleId, ev) {
        await this.orm.write(
            "nsp.measurement.validation.vehicle",
            [vehicleId],
            { retry_selected: Boolean(ev.target.checked) }
        );
        await this.refresh();
    }

    async newRunAll(runId) {
        const action = await this.callModel("nsp.measurement.validation.run", "action_new_run_all", [runId]);
        if (action?.type) {
            await this.action.doAction(action);
        }
    }

    async selectRun(ev) {
        const id = Number(ev.target.value || 0);
        this.state.selectedRunId = Number.isInteger(id) && id > 0 ? id : false;
        await this.refresh();
    }

    setTab(tab) {
        this.state.activeTab = tab;
    }

    tabClass(tab) {
        return this.state.activeTab === tab ? "nsp-cw__tab nsp-cw__tab--active" : "nsp-cw__tab";
    }

    formatDateTime(value) {
        if (!value) {
            return "—";
        }
        const normalized = String(value).includes("T") ? String(value) : `${String(value).replace(" ", "T")}Z`;
        const date = new Date(normalized);
        if (Number.isNaN(date.getTime())) {
            return String(value);
        }
        return new Intl.DateTimeFormat(undefined, {
            day: "2-digit", month: "2-digit", year: "numeric",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
        }).format(date);
    }

    formatNumber(value, digits = 1) {
        const number = Number(value || 0);
        return Number.isFinite(number) ? number.toFixed(digits) : "0";
    }

    resultLabel(value) {
        const labels = {
            pending: "Pending",
            complete: "Complete",
            incomplete: "Incomplete",
            not_detected: "Not Detected",
            wrong_order: "Wrong Order",
            transition_timeout: "Transition Timeout",
            insufficient: "Insufficient",
        };
        return labels[value] || value || "—";
    }

    resultClass(value) {
        if (value === "complete" || value === "accepted" || value === "passed") {
            return "nsp-cw__badge nsp-cw__badge--success";
        }
        if (["incomplete", "transition_timeout", "completed"].includes(value)) {
            return "nsp-cw__badge nsp-cw__badge--warning";
        }
        if (["not_detected", "wrong_order", "rejected", "failed"].includes(value)) {
            return "nsp-cw__badge nsp-cw__badge--danger";
        }
        if (value === "running") {
            return "nsp-cw__badge nsp-cw__badge--info";
        }
        return "nsp-cw__badge";
    }

    statusLabel(value) {
        const labels = {
            draft: "Draft", ready: "Ready", running: "Running", completed: "Completed",
            applied: "Applied", failed: "Failed", cancelled: "Cancelled",
            validation: "Ready for Validation", accepted: "Accepted", superseded: "Superseded",
            passed: "Passed", pending: "Pending",
        };
        return labels[value] || value || "—";
    }

    activeRun() {
        return this.state.data.active_validation_run || null;
    }

    currentResult() {
        return this.state.data.current_result || this.state.data.accepted_result || this.state.data.draft_result || null;
    }



    portWidth(stat) {
        return `${Math.max(0, Math.min(100, Number(stat?.detection_rate || 0)))}%`;
    }

    distributionStyle() {
        const run = this.activeRun();
        if (!run?.expected_count) {
            return "background: conic-gradient(#d9e2ef 0 100%)";
        }
        const total = Number(run.expected_count || 1);
        const complete = Number(run.complete_count || 0) * 100 / total;
        const incomplete = Number(run.incomplete_count || 0) * 100 / total;
        const missing = Number(run.not_detected_count || 0) * 100 / total;
        const wrong = Number(run.wrong_order_count || 0) * 100 / total;
        const a = complete;
        const b = a + incomplete;
        const c = b + missing;
        const d = Math.min(100, c + wrong);
        return `background: conic-gradient(#22a35a 0 ${a}%, #f59e0b ${a}% ${b}%, #dc3545 ${b}% ${c}%, #7c3aed ${c}% ${d}%, #d9e2ef ${d}% 100%)`;
    }

    passSequence(pass) {
        return pass?.detected_sequence || "No detection sequence";
    }

}

registry.category("fields").add("nsp_calibration_workspace", {
    component: NspCalibrationWorkspace,
    supportedTypes: ["boolean"],
});
