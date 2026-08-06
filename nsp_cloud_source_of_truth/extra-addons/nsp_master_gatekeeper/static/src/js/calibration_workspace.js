/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { NspInfrastructureScopeDialog } from "./infrastructure_scope_dialog";

export class NspCalibrationWorkspace extends Component {
    static template = "nsp_master_gatekeeper.CalibrationWorkspace";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.sessionId = this.resolveSessionId();
        this.timer = null;
        this.loading = false;
        this.refreshPending = false;
        this.state = useState({
            data: { found: false, steps: [], vehicles: [], readers: [] },
            selectedStepIds: [],
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

    async refresh({ force = false } = {}) {
        if (!this.sessionId) {
            return;
        }
        if (this.loading) {
            if (force) {
                this.refreshPending = true;
            }
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
                this.state.error = "Lane Calibration was not found.";
                return;
            }
            this.state.data = data;
            const available = new Set(
                (data.steps || []).map((step) => Number(step.first_event_id || 0))
            );
            this.state.selectedStepIds = this.state.selectedStepIds.filter((id) => available.has(id));
            this.state.error = "";
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || "Unable to load Lane Calibration.";
        } finally {
            this.loading = false;
            if (this.refreshPending) {
                this.refreshPending = false;
                window.setTimeout(() => this.refresh(), 0);
            }
        }
    }

    async callSession(method, args = []) {
        try {
            return await this.orm.call(
                "nsp.measurement.session",
                method,
                [[this.sessionId], ...args]
            );
        } catch (error) {
            this.notification.add(
                error?.data?.message || error?.message || "The operation could not be completed.",
                { type: "danger" }
            );
            return false;
        }
    }

    async openCard(kind) {
        if (kind === "infrastructure") {
            this.dialog.add(NspInfrastructureScopeDialog, {
                sessionId: this.sessionId,
                onChanged: async () => {
                    await this.refresh({ force: true });
                    if (typeof this.props.record?.load === "function") {
                        await this.props.record.load();
                    }
                },
            });
            return;
        }
        const methods = {
            vehicles: "action_open_vehicles_card",
            coverage: "action_open_rfid_coverage_card",
        };
        const method = methods[kind];
        if (!method) {
            return;
        }
        const action = await this.callSession(method);
        if (action?.type) {
            const refreshWorkspace = async () => {
                await this.refresh({ force: true });
                if (typeof this.props.record?.load === "function") {
                    await this.props.record.load();
                }
            };
            await this.action.doAction(action, { onClose: refreshWorkspace });
        }
    }

    toggleStep(step, ev) {
        const id = Number(step.first_event_id || 0);
        if (!id) {
            return;
        }
        const index = this.state.selectedStepIds.indexOf(id);
        if (ev.target.checked && index < 0) {
            this.state.selectedStepIds.push(id);
        } else if (!ev.target.checked && index >= 0) {
            this.state.selectedStepIds.splice(index, 1);
        }
    }

    isSelected(step) {
        return this.state.selectedStepIds.includes(Number(step.first_event_id || 0));
    }

    selectionOrder(step) {
        const index = this.state.selectedStepIds.indexOf(Number(step.first_event_id || 0));
        return index >= 0 ? index + 1 : "";
    }

    canApply() {
        return ["ready", "running", "completed"].includes(this.state.data.status)
            && this.state.selectedStepIds.length >= 2;
    }

    async applyConfiguration() {
        if (this.state.selectedStepIds.length < 2) {
            this.notification.add("Select at least two Detection Timeline rows.", { type: "warning" });
            return;
        }
        const action = await this.callSession(
            "action_open_apply_configuration",
            [Array.from(this.state.selectedStepIds)]
        );
        if (action?.type) {
            const refreshAfterSave = async (closeInfo = {}) => {
                // Odoo 19 forwards ir.actions.act_window_close.infos to onClose.
                // Refresh only after the wizard confirmed a successful Save;
                // Cancel closes with { special: true } and must not alter selection.
                if (!closeInfo?.refresh_lane_calibration) {
                    return;
                }
                this.state.selectedStepIds.splice(0, this.state.selectedStepIds.length);
                await this.refresh({ force: true });
                if (typeof this.props.record?.load === "function") {
                    await this.props.record.load();
                }
                const laneName = closeInfo.lane_name || "Lane";
                this.notification.add(`${laneName} configuration saved.`, { type: "success" });
            };
            await this.action.doAction(action, { onClose: refreshAfterSave });
        }
    }

    async clearTimeline() {
        if (!window.confirm("Clear all detections in the current Detection Timeline?")) {
            return;
        }
        const result = await this.callSession("action_clear_detection_timeline");
        if (result !== false) {
            this.state.selectedStepIds.splice(0, this.state.selectedStepIds.length);
            this.notification.add("Detection Timeline cleared.", { type: "success" });
            await this.refresh();
        }
    }

    formatDateTime(value) {
        if (!value) {
            return "—";
        }
        const normalized = String(value).includes("T")
            ? String(value)
            : `${String(value).replace(" ", "T")}Z`;
        const date = new Date(normalized);
        if (Number.isNaN(date.getTime())) {
            return String(value);
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

    formatNumber(value, digits = 1) {
        const number = Number(value || 0);
        return Number.isFinite(number) ? number.toFixed(digits) : "0";
    }

    statusLabel(value) {
        const labels = {
            draft: "Draft",
            ready: "Ready",
            running: "Running",
            completed: "Completed",
            applied: "Configured",
            cancelled: "Cancelled",
            failed: "Failed",
        };
        return labels[value] || value || "—";
    }

    statusClass(value) {
        if (value === "applied") {
            return "nsp-lc__status nsp-lc__status--success";
        }
        if (value === "running") {
            return "nsp-lc__status nsp-lc__status--live";
        }
        if (["cancelled", "failed"].includes(value)) {
            return "nsp-lc__status nsp-lc__status--danger";
        }
        return "nsp-lc__status";
    }
}

registry.category("fields").add("nsp_calibration_workspace", {
    component: NspCalibrationWorkspace,
    supportedTypes: ["boolean"],
});
