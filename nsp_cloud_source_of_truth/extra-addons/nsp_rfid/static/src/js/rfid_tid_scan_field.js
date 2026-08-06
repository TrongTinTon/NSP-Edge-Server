/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";

import { playScanTone, refocusAndSelect } from "@nsp_rfid/js/scan_feedback";
import { normalizeRfidTid } from "@nsp_rfid/js/tid_normalizer";

export class NspRfidTidScanField extends Component {
    static template = "nsp_rfid.RfidTidScanField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
        autoFocus: { type: Boolean, optional: true },
    };
    static defaultProps = { placeholder: "", autoFocus: true };

    setup() {
        this.orm = useService("orm");
        this.input = useRef("input");
        this.timer = null;
        this.requestSequence = 0;
        this.state = useState({
            value: normalizeRfidTid(this.props.record.data[this.props.name]),
            status: "idle",
            message: _t("Ready to scan RFID TID."),
        });

        useEffect(
            () => this.syncFromRecord(),
            () => [this.props.record.data[this.props.name] || ""]
        );
        onMounted(() => {
            if (this.props.autoFocus && !this.props.readonly && !this.props.record.resId) {
                refocusAndSelect(this.input);
            }
        });
        onWillUnmount(() => this.clearTimer());
    }

    syncFromRecord() {
        const value = normalizeRfidTid(this.props.record.data[this.props.name]);
        if (this.state.status === "validating" || value === this.state.value) {
            return;
        }
        this.state.value = value;
        this.state.status = value ? "success" : "idle";
        this.state.message = value
            ? _t("TID is normalized.")
            : _t("Ready to scan RFID TID.");
    }

    get statusClass() {
        return { [`nsp-rfid-tid-scan--${this.state.status}`]: true };
    }

    get statusIcon() {
        return {
            success: "fa fa-check-circle",
            error: "fa fa-exclamation-circle",
            validating: "fa fa-circle-o-notch fa-spin",
        }[this.state.status] || "fa fa-barcode";
    }

    clearTimer() {
        if (this.timer) {
            window.clearTimeout(this.timer);
            this.timer = null;
        }
    }

    async updateValue(value) {
        const canonical = normalizeRfidTid(value);
        if (canonical !== this.props.record.data[this.props.name]) {
            await this.props.record.update({ [this.props.name]: canonical || false });
        }
        return canonical;
    }

    async onInput(event) {
        this.clearTimer();
        const canonical = normalizeRfidTid(event.target.value);
        event.target.value = canonical;
        this.state.value = canonical;
        this.state.status = canonical ? "scanning" : "idle";
        this.state.message = canonical
            ? _t("Reading and normalizing TID…")
            : _t("Ready to scan RFID TID.");
        await this.updateValue(canonical);
        if (event.inputType === "insertFromPaste") {
            this.scheduleValidation(0);
        }
    }

    onPaste() {
        this.scheduleValidation(0);
    }

    onFocus(event) {
        event.target.select();
    }

    onBlur() {
        if (this.state.value && this.state.status !== "success") {
            this.scheduleValidation(0, false);
        }
    }

    async onKeydown(event) {
        if (!["Enter", "Tab"].includes(event.key)) {
            return;
        }
        if (event.key === "Tab" && !this.state.value) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        await this.validateTid();
    }

    scheduleValidation(delay = 160, refocus = true) {
        this.clearTimer();
        this.timer = window.setTimeout(() => {
            this.timer = null;
            void this.validateTid({ refocus });
        }, delay);
    }

    async validateTid({ refocus = true } = {}) {
        const tid = await this.updateValue(this.state.value);
        if (!tid) {
            this.state.status = "idle";
            this.state.message = _t("Ready to scan RFID TID.");
            return;
        }

        const sequence = ++this.requestSequence;
        this.state.status = "validating";
        this.state.message = _t("Checking TID format and uniqueness…");
        let result;
        try {
            result = await this.orm.call(
                "nsp.rfid.tag",
                "nsp_validate_new_tid",
                [tid],
                { current_id: this.props.record.resId || false }
            );
        } catch (error) {
            if (sequence === this.requestSequence) {
                this.setResult(
                    false,
                    error?.data?.message
                        || error?.message
                        || _t("TID validation failed."),
                    refocus
                );
            }
            return;
        }
        if (sequence !== this.requestSequence) {
            return;
        }

        this.state.value = result?.tid || tid;
        await this.updateValue(this.state.value);
        this.setResult(
            Boolean(result?.valid),
            result?.message || (result?.valid ? _t("TID is available.") : _t("TID is invalid.")),
            refocus
        );
    }

    setResult(success, message, refocus) {
        this.state.status = success ? "success" : "error";
        this.state.message = message;
        playScanTone(success);
        if (refocus) {
            refocusAndSelect(this.input);
        }
    }
}

export const nspRfidTidScanField = {
    component: NspRfidTidScanField,
    displayName: _t("RFID TID Scanner"),
    supportedTypes: ["char"],
    supportedOptions: [
        { label: _t("Auto Focus"), name: "autofocus", type: "boolean" },
    ],
    extractProps: ({ options, placeholder }) => ({
        placeholder,
        autoFocus: options.autofocus !== false,
    }),
};

registry.category("fields").add("nsp_rfid_tid_scan", nspRfidTidScanField);
