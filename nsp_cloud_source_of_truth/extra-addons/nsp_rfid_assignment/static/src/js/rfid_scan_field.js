/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";

import { playScanTone, refocusAndSelect } from "@nsp_rfid/js/scan_feedback";
import { normalizeRfidTid } from "@nsp_rfid/js/tid_normalizer";

function many2OneId(value) {
    if (!value) {
        return false;
    }
    if (Array.isArray(value)) {
        return value[0] || false;
    }
    if (typeof value === "object") {
        return value.id || value.resId || false;
    }
    return value;
}

function many2OneLabel(value) {
    if (!value) {
        return "";
    }
    if (Array.isArray(value)) {
        return value[1] || "";
    }
    if (typeof value === "object") {
        return value.display_name || value.name || "";
    }
    return "";
}

export class NspRfidScanField extends Component {
    static template = "nsp_rfid_assignment.RfidScanField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
        targetField: { type: String },
        resolvedValueField: { type: String, optional: true },
        expectedTarget: { type: String, optional: true },
        requireAvailable: { type: Boolean, optional: true },
        createMissing: { type: Boolean, optional: true },
        requireActiveAssignment: { type: Boolean, optional: true },
        autoFocus: { type: Boolean, optional: true },
        autoNextRow: { type: Boolean, optional: true },
        skipDuplicates: { type: Boolean, optional: true },
        nextScanField: { type: String, optional: true },
    };
    static defaultProps = {
        placeholder: "",
        resolvedValueField: "",
        expectedTarget: "",
        requireAvailable: false,
        createMissing: false,
        requireActiveAssignment: false,
        autoFocus: false,
        autoNextRow: false,
        skipDuplicates: false,
        nextScanField: "",
    };

    setup() {
        this.orm = useService("orm");
        this.input = useRef("input");
        this.timer = null;
        this.state = useState({
            value: normalizeRfidTid(this.externalValue),
            status: "idle",
            message: "",
        });

        useEffect(
            () => this.syncFromRecord(),
            () => [
                this.props.record.data[this.props.name] || "",
                this.props.resolvedValueField
                    ? this.props.record.data[this.props.resolvedValueField] || ""
                    : "",
                many2OneId(this.resolvedValue) || false,
            ]
        );
        onMounted(() => {
            if (this.props.autoFocus && !this.props.readonly) {
                refocusAndSelect(this.input);
            }
        });
        onWillUnmount(() => this.clearTimer());
    }

    get resolvedValue() {
        return this.props.record.data[this.props.targetField];
    }

    get externalValue() {
        const directValue = this.props.record.data[this.props.name];
        const resolvedValue = this.props.resolvedValueField
            ? this.props.record.data[this.props.resolvedValueField]
            : "";
        return directValue || resolvedValue || "";
    }

    get resolvedLabel() {
        return many2OneLabel(this.resolvedValue);
    }

    get readonlyLabel() {
        return normalizeRfidTid(this.externalValue) || this.resolvedLabel;
    }

    get statusClass() {
        return {
            "nsp-rfid-scan--validating": this.state.status === "validating",
            "nsp-rfid-scan--success": this.state.status === "success",
            "nsp-rfid-scan--error": this.state.status === "error",
        };
    }

    syncFromRecord() {
        const value = normalizeRfidTid(this.externalValue);
        if (this.state.status === "validating" || value === this.state.value) {
            return;
        }
        this.state.value = value;
        this.state.status = "idle";
        this.state.message = "";
    }

    clearTimer() {
        if (this.timer) {
            window.clearTimeout(this.timer);
            this.timer = null;
        }
    }

    onInput(event) {
        this.clearTimer();
        let value = event.target.value;
        if (
            this.state.status === "success"
            && this.state.value
            && value.startsWith(this.state.value)
            && value.length > this.state.value.length
        ) {
            value = value.slice(this.state.value.length);
        }
        value = normalizeRfidTid(value);
        event.target.value = value;
        this.state.value = value;
        this.state.status = "idle";
        this.state.message = "";
        if (event.inputType === "insertFromPaste") {
            this.scheduleValidation();
        }
    }

    onPaste() {
        this.scheduleValidation();
    }

    onFocus(event) {
        event.target.select();
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
        await this.validateScan();
    }

    scheduleValidation() {
        this.clearTimer();
        this.timer = window.setTimeout(() => {
            this.timer = null;
            void this.validateScan();
        }, 0);
    }

    async validateScan() {
        if (this.state.status === "validating") {
            return;
        }

        const tid = normalizeRfidTid(this.state.value);
        if (!tid) {
            await this.setInvalid(_t("Scan or enter an RFID TID first."));
            return;
        }
        this.state.value = tid;

        if (this.shouldCheckDuplicates && this.findDuplicateInput(tid)) {
            await this.skipDuplicateScan();
            return;
        }

        this.state.status = "validating";
        this.state.message = "";
        let result;
        try {
            result = await this.orm.call(
                "nsp.rfid.tag",
                "nsp_validate_scan",
                [tid],
                {
                    expected_target: this.props.expectedTarget || false,
                    require_available: Boolean(this.props.requireAvailable),
                    allow_tag_id: many2OneId(this.resolvedValue) || false,
                    create_missing: Boolean(this.props.createMissing),
                    require_active_assignment: Boolean(
                        this.props.requireActiveAssignment
                    ),
                }
            );
        } catch (error) {
            await this.setInvalid(
                error?.data?.message || error?.message || _t("RFID validation failed.")
            );
            return;
        }

        if (!result?.valid || !result.tag_id) {
            await this.setInvalid(result?.message || _t("RFID Tag is not valid."));
            return;
        }
        if (this.shouldCheckDuplicates && this.findDuplicateInput(result.tid)) {
            await this.skipDuplicateScan();
            return;
        }

        await this.props.record.update({ [this.props.name]: result.tid });
        this.state.value = result.tid;
        this.state.status = "success";
        this.state.message = "";
        playScanTone(true);
        this.moveAfterSuccess();
    }

    get shouldCheckDuplicates() {
        return this.props.autoNextRow || this.props.skipDuplicates;
    }

    findDuplicateInput(tid) {
        const currentInput = this.input.el;
        const container = currentInput?.closest(
            ".o_field_x2many, .o_list_renderer, .o_list_view"
        );
        if (!container) {
            return false;
        }
        return Array.from(container.querySelectorAll(".nsp-rfid-scan__input")).find(
            (input) =>
                input !== currentInput
                && input.dataset.fieldName === this.props.name
                && normalizeRfidTid(input.value) === tid
        ) || false;
    }

    async skipDuplicateScan() {
        const hasResolvedTag = Boolean(many2OneId(this.resolvedValue));
        const resolvedValue = this.props.resolvedValueField
            ? this.props.record.data[this.props.resolvedValueField]
            : false;
        const updates = {
            [this.props.name]: hasResolvedTag ? resolvedValue || false : false,
        };
        if (!hasResolvedTag) {
            updates[this.props.targetField] = false;
        }
        await this.props.record.update(updates);
        this.state.value = hasResolvedTag ? normalizeRfidTid(resolvedValue) : "";
        this.state.status = "idle";
        this.state.message = "";
        refocusAndSelect(this.input);
    }

    async setInvalid(message) {
        const updates = { [this.props.name]: normalizeRfidTid(this.state.value) };
        if (!many2OneId(this.resolvedValue)) {
            updates[this.props.targetField] = false;
        }
        await this.props.record.update(updates);
        this.state.status = "error";
        this.state.message = message;
        playScanTone(false);
        refocusAndSelect(this.input);
    }

    moveAfterSuccess() {
        if (this.props.nextScanField) {
            this.focusNamedField();
        } else if (this.props.autoNextRow) {
            this.focusNextRow();
        } else {
            refocusAndSelect(this.input);
        }
    }

    focusNamedField() {
        window.setTimeout(() => {
            const row = this.input.el?.closest("tr.o_data_row");
            const nextInput = row
                ? Array.from(row.querySelectorAll(".nsp-rfid-scan__input")).find(
                    (input) =>
                        input.dataset.fieldName === this.props.nextScanField
                        && !input.disabled
                )
                : false;
            if (nextInput) {
                nextInput.focus();
                nextInput.select();
            } else {
                refocusAndSelect(this.input);
            }
        }, 0);
    }

    focusNextRow() {
        window.setTimeout(() => {
            const currentInput = this.input.el;
            const currentRow = currentInput?.closest("tr.o_data_row");
            const container = currentInput?.closest(
                ".o_field_x2many, .o_list_renderer, .o_list_view"
            );
            if (!currentRow || !container) {
                refocusAndSelect(this.input);
                return;
            }

            const rows = Array.from(container.querySelectorAll("tr.o_data_row"));
            const nextInput = rows
                .slice(rows.indexOf(currentRow) + 1)
                .map((row) => row.querySelector(".nsp-rfid-scan__input"))
                .find((input) => input && !input.disabled);
            if (nextInput) {
                nextInput.focus();
                nextInput.select();
                return;
            }

            const addLine = container.querySelector(
                "tr.o_field_x2many_list_row_add a, "
                + ".o_field_x2many_list_row_add a, "
                + ".o_list_add_row a, .o_list_button_add"
            );
            if (!addLine) {
                refocusAndSelect(this.input);
                return;
            }

            const previousCount = container.querySelectorAll(
                ".nsp-rfid-scan__input"
            ).length;
            currentInput.blur();
            addLine.click();
            this.focusCreatedRow(container, previousCount, 0);
        }, 0);
    }

    focusCreatedRow(container, previousCount, attempt) {
        window.setTimeout(() => {
            const inputs = Array.from(
                container.querySelectorAll(".nsp-rfid-scan__input")
            ).filter((input) => !input.disabled && input.offsetParent !== null);
            const blankInput = inputs.find(
                (input, index) => index >= previousCount && !input.value.trim()
            ) || [...inputs].reverse().find(
                (input) => input !== this.input.el && !input.value.trim()
            );
            if (blankInput) {
                blankInput.focus();
                blankInput.select();
            } else if (attempt < 11) {
                this.focusCreatedRow(container, previousCount, attempt + 1);
            } else {
                refocusAndSelect(this.input);
            }
        }, 50);
    }
}

export const nspRfidScanField = {
    component: NspRfidScanField,
    displayName: _t("RFID Keyboard Scan"),
    supportedTypes: ["char"],
    supportedOptions: [
        { label: _t("Target Many2one Field"), name: "target_field", type: "string" },
        { label: _t("Resolved Value Field"), name: "resolved_value_field", type: "string" },
        { label: _t("Expected Assignment Target"), name: "expected_target", type: "string" },
        { label: _t("Require Available Tag"), name: "require_available", type: "boolean" },
        { label: _t("Create Missing Whitelist Tag"), name: "create_missing", type: "boolean" },
        { label: _t("Require Active Assignment"), name: "require_active_assignment", type: "boolean" },
        { label: _t("Auto Focus"), name: "autofocus", type: "boolean" },
        { label: _t("Auto Next Row"), name: "auto_next_row", type: "boolean" },
        { label: _t("Skip Duplicate TID"), name: "skip_duplicates", type: "boolean" },
        { label: _t("Next Scan Field"), name: "next_scan_field", type: "string" },
    ],
    extractProps: ({ options, placeholder }) => ({
        placeholder,
        targetField: options.target_field,
        resolvedValueField: options.resolved_value_field || "",
        expectedTarget: options.expected_target || "",
        requireAvailable: Boolean(options.require_available),
        createMissing: Boolean(options.create_missing),
        requireActiveAssignment: Boolean(options.require_active_assignment),
        autoFocus: Boolean(options.autofocus),
        autoNextRow: Boolean(options.auto_next_row),
        skipDuplicates: Boolean(options.skip_duplicates),
        nextScanField: options.next_scan_field || "",
    }),
};

registry.category("fields").add("nsp_rfid_scan", nspRfidScanField);
