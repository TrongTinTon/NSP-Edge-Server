/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, useRef, useState } from "@odoo/owl";

function extractMany2OneId(value) {
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

function extractMany2OneLabel(value) {
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
    static template = "nsp_rfid.RfidScanField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
        targetField: { type: String },
        resolvedValueField: { type: String, optional: true },
        expectedCardType: { type: String, optional: true },
        requireAvailable: { type: Boolean, optional: true },
        autoFocus: { type: Boolean, optional: true },
        autoNextRow: { type: Boolean, optional: true },
    };
    static defaultProps = {
        placeholder: "",
        expectedCardType: "",
        requireAvailable: false,
        autoFocus: false,
        autoNextRow: false,
    };

    setup() {
        this.orm = useService("orm");
        this.input = useRef("input");
        const resolvedInputValue = this.props.resolvedValueField
            ? this.props.record.data[this.props.resolvedValueField]
            : "";
        this.state = useState({
            value: this.props.record.data[this.props.name] || resolvedInputValue || "",
            status: "idle",
            message: "",
        });

        onMounted(() => {
            if (this.props.autoFocus && !this.props.readonly) {
                this.input.el?.focus();
            }
        });
    }

    get statusClass() {
        return {
            "nsp-rfid-scan--validating": this.state.status === "validating",
            "nsp-rfid-scan--success": this.state.status === "success",
            "nsp-rfid-scan--error": this.state.status === "error",
        };
    }

    get resolvedValue() {
        return this.props.record.data[this.props.targetField];
    }

    get resolvedLabel() {
        return extractMany2OneLabel(this.resolvedValue);
    }

    get readonlyLabel() {
        const resolvedInputValue = this.props.resolvedValueField
            ? this.props.record.data[this.props.resolvedValueField]
            : "";
        return resolvedInputValue || this.resolvedLabel || this.state.value || "";
    }

    normalizeTid(value) {
        return String(value || "").trim().toUpperCase().replaceAll(" ", "");
    }

    onInput(event) {
        let value = event.target.value;

        // A keyboard RFID reader can start the next scan before the browser has
        // restored the text selection after a successful validation. In that
        // case, discard the previously validated TID instead of appending the
        // new scan to it.
        if (this.state.status === "success" && this.state.value) {
            const previous = String(this.state.value);
            if (value.startsWith(previous) && value.length > previous.length) {
                value = value.slice(previous.length);
                event.target.value = value;
            }
        }

        this.state.value = value;
        if (this.state.status !== "validating") {
            this.state.status = "idle";
            this.state.message = "";
        }
    }

    onFocus(event) {
        event.target.select();
    }

    async onKeydown(event) {
        if (!(["Enter", "Tab"].includes(event.key))) {
            return;
        }
        if (event.key === "Tab" && !this.state.value) {
            return;
        }
        event.preventDefault();
        event.stopPropagation();
        await this.validateScan();
    }

    async validateScan() {
        if (this.state.status === "validating") {
            return;
        }

        const tid = this.normalizeTid(this.state.value);
        if (!tid) {
            await this.setInvalid(_t("Scan or enter an RFID TID first."));
            return;
        }

        this.state.value = tid;

        // User/Vehicle card lists are append-only scan workflows. When the
        // same TID is already present in an earlier row, ignore the repeated
        // scan and keep the current blank row ready for the next card. Do not
        // validate again and, importantly, do not create another line.
        if (this.props.autoNextRow) {
            const duplicateInput = this.findDuplicateInput(tid);
            if (duplicateInput) {
                await this.skipDuplicateScan();
                return;
            }
        }

        this.state.status = "validating";
        this.state.message = "";

        const allowCardId = extractMany2OneId(this.resolvedValue);
        let result;
        try {
            result = await this.orm.call(
                "nsp.rfid.card",
                "nsp_validate_scan",
                [tid],
                {
                    expected_card_type: this.props.expectedCardType || false,
                    require_available: Boolean(this.props.requireAvailable),
                    allow_card_id: allowCardId || false,
                }
            );
        } catch (error) {
            const message = error?.data?.message || error?.message || _t("RFID validation failed.");
            await this.setInvalid(message);
            return;
        }

        if (!result?.valid || !result.card_id) {
            await this.setInvalid(result?.message || _t("RFID tag is not valid."));
            return;
        }

        // Re-check using the canonical TID returned by the server. This also
        // covers scanners that include formatting characters stripped by the
        // backend normalizer.
        if (this.props.autoNextRow && this.findDuplicateInput(result.tid)) {
            await this.skipDuplicateScan();
            return;
        }

        await this.props.record.update({
            [this.props.name]: result.tid,
            [this.props.targetField]: result.card_id,
        });
        this.state.value = result.tid;
        this.state.status = "success";
        this.state.message = "";
        this.playTone(true);
        if (this.props.autoNextRow) {
            this.advanceToNextRow();
        } else {
            this.refocus();
        }
    }

    findDuplicateInput(tid) {
        const currentInput = this.input.el;
        if (!currentInput) {
            return false;
        }
        const fieldRoot = currentInput.closest(".o_field_x2many") ||
            currentInput.closest(".o_list_renderer") ||
            currentInput.closest(".o_list_view");
        if (!fieldRoot) {
            return false;
        }
        const normalizedTid = this.normalizeTid(tid);
        return Array.from(fieldRoot.querySelectorAll(".nsp-rfid-scan__input")).find((input) =>
            input !== currentInput && this.normalizeTid(input.value) === normalizedTid
        ) || false;
    }

    async skipDuplicateScan() {
        const resolvedInputValue = this.props.resolvedValueField
            ? this.props.record.data[this.props.resolvedValueField]
            : "";
        const hasResolvedCard = Boolean(extractMany2OneId(this.resolvedValue));

        // A repeated scan normally happens on the auto-created blank row.
        // Clear that virtual row so it remains reusable and is stripped before
        // save. Existing valid rows are restored instead of being destroyed.
        await this.props.record.update({
            [this.props.name]: hasResolvedCard ? (resolvedInputValue || false) : false,
            ...(hasResolvedCard ? {} : { [this.props.targetField]: false }),
        });
        this.state.value = hasResolvedCard ? (resolvedInputValue || "") : "";
        this.state.status = "idle";
        this.state.message = "";
        this.refocus();
    }

    async setInvalid(message) {
        const updates = {
            [this.props.name]: this.normalizeTid(this.state.value),
        };
        // Do not destroy an already-valid relation when a later scan is invalid.
        // New rows have no target yet, so they remain safely unresolved.
        if (!extractMany2OneId(this.resolvedValue)) {
            updates[this.props.targetField] = false;
        }
        await this.props.record.update(updates);
        this.state.status = "error";
        this.state.message = message;
        this.playTone(false);
        this.refocus();
    }


    advanceToNextRow() {
        window.setTimeout(() => {
            const currentInput = this.input.el;
            if (!currentInput) {
                return;
            }

            const currentRow = currentInput.closest("tr.o_data_row");
            const fieldRoot = currentInput.closest(".o_field_x2many") ||
                currentInput.closest(".o_list_renderer") ||
                currentInput.closest(".o_list_view");

            if (!currentRow || !fieldRoot) {
                this.refocus();
                return;
            }

            const rows = Array.from(fieldRoot.querySelectorAll("tr.o_data_row"));
            const currentIndex = rows.indexOf(currentRow);
            const nextInput = rows.slice(currentIndex + 1)
                .map((row) => row.querySelector(".nsp-rfid-scan__input"))
                .find((element) => element && !element.disabled);

            if (nextInput) {
                nextInput.focus();
                nextInput.select();
                return;
            }

            const previousCount = fieldRoot.querySelectorAll(".nsp-rfid-scan__input").length;
            const addLine = fieldRoot.querySelector(
                "tr.o_field_x2many_list_row_add a, " +
                ".o_field_x2many_list_row_add a, " +
                ".o_list_add_row a, " +
                ".o_list_button_add"
            );

            if (!addLine) {
                this.refocus();
                return;
            }

            currentInput.blur();
            addLine.click();

            let attempts = 0;
            const focusCreatedRow = () => {
                attempts += 1;
                const inputs = Array.from(fieldRoot.querySelectorAll(".nsp-rfid-scan__input"))
                    .filter((element) => !element.disabled && element.offsetParent !== null);
                const blankInput = inputs.find((element, index) =>
                    index >= previousCount && !String(element.value || "").trim()
                ) || [...inputs].reverse().find((element) =>
                    element !== currentInput && !String(element.value || "").trim()
                );

                if (blankInput) {
                    blankInput.focus();
                    blankInput.select();
                    return;
                }
                if (attempts < 12) {
                    window.setTimeout(focusCreatedRow, 50);
                } else {
                    this.refocus();
                }
            };
            window.setTimeout(focusCreatedRow, 50);
        }, 0);
    }

    refocus() {
        window.setTimeout(() => {
            if (this.input.el) {
                this.input.el.focus();
                this.input.el.select();
            }
        }, 0);
    }

    playTone(success) {
        try {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) {
                return;
            }
            const context = new AudioContext();
            const oscillator = context.createOscillator();
            const gain = context.createGain();
            oscillator.type = "sine";
            oscillator.frequency.value = success ? 880 : 220;
            gain.gain.setValueAtTime(0.0001, context.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.12);
            oscillator.connect(gain);
            gain.connect(context.destination);
            oscillator.start();
            oscillator.stop(context.currentTime + 0.13);
            oscillator.addEventListener("ended", () => context.close());
        } catch {
            // Visual feedback remains the source of truth when audio is blocked.
        }
    }
}

export const nspRfidScanField = {
    component: NspRfidScanField,
    displayName: _t("RFID Keyboard Scan"),
    supportedTypes: ["char"],
    supportedOptions: [
        { label: _t("Target Many2one Field"), name: "target_field", type: "string" },
        { label: _t("Resolved Value Field"), name: "resolved_value_field", type: "string" },
        { label: _t("Expected Card Type"), name: "expected_card_type", type: "string" },
        { label: _t("Require Available Card"), name: "require_available", type: "boolean" },
        { label: _t("Auto Focus"), name: "autofocus", type: "boolean" },
        { label: _t("Auto Next Row"), name: "auto_next_row", type: "boolean" },
    ],
    extractProps: ({ options, placeholder }) => ({
        placeholder,
        targetField: options.target_field,
        resolvedValueField: options.resolved_value_field || "",
        expectedCardType: options.expected_card_type || "",
        requireAvailable: Boolean(options.require_available),
        autoFocus: Boolean(options.autofocus),
        autoNextRow: Boolean(options.auto_next_row),
    }),
};

registry.category("fields").add("nsp_rfid_scan", nspRfidScanField);
