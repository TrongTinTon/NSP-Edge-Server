/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

/**
 * DynamicFieldInput renders a single form field based on its schema descriptor.
 *
 * Props:
 *   schema  — { key, type, label, required, default, validator, placeholder }
 *   value   — current value (any shape)
 *   widgets — Array of widget objects [{id, data: {title}}] for widget_picker type
 *   onChange — (newValue) => void
 */
export class DynamicFieldInput extends Component {
    static template = "t4_custom_dashboard.DynamicFieldInput";
    static props = {
        schema: { type: Object },
        value: { optional: true },
        widgets: { type: Array, optional: true },
        onChange: { type: Function },
    };

    setup() {
        this.state = useState({
            jsonError: null,
            rawJson: this._valueToJsonText(this.props.value),
        });
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    _valueToJsonText(val) {
        if (val === undefined || val === null) return "";
        if (typeof val === "string") return val;
        try {
            return JSON.stringify(val, null, 2);
        } catch (e) {
            return String(val);
        }
    }

    // -------------------------------------------------------------------------
    // Char input
    // -------------------------------------------------------------------------

    onCharInput(ev) {
        this.props.onChange(ev.target.value);
    }

    // -------------------------------------------------------------------------
    // Bool input
    // -------------------------------------------------------------------------

    onBoolChange(ev) {
        this.props.onChange(ev.target.checked);
    }

    // -------------------------------------------------------------------------
    // JSON textarea
    // -------------------------------------------------------------------------

    onJsonInput(ev) {
        this.state.rawJson = ev.target.value;
    }

    onJsonBlur() {
        const raw = this.state.rawJson;
        if (!raw || raw.trim() === "") {
            this.state.jsonError = null;
            this.props.onChange(null);
            return;
        }
        try {
            const parsed = JSON.parse(raw);
            this.state.jsonError = null;
            this.props.onChange(parsed);
        } catch (e) {
            // Fallback: a bare scalar (e.g. `day`) is not valid JSON but is a
            // legitimate string value for fields like `default`. Only surface a
            // parse error when the input clearly intends to be JSON.
            const trimmed = raw.trim();
            const looksLikeJson =
                /^[\[{"]/.test(trimmed) ||
                /^-?\d/.test(trimmed) ||
                /^(true|false|null)$/.test(trimmed);
            if (!looksLikeJson) {
                this.state.jsonError = null;
                this.props.onChange(raw);
                return;
            }
            this.state.jsonError = "JSON không hợp lệ: " + e.message;
        }
    }

    // -------------------------------------------------------------------------
    // Widget picker (multi-select chips)
    // -------------------------------------------------------------------------

    get widgetList() {
        return this.props.widgets || [];
    }

    /** true = "all widgets" (null stored), false = specific selection */
    get isAllWidgets() {
        return this.props.value === null || this.props.value === undefined;
    }

    get selectedWidgetIds() {
        const val = this.props.value;
        if (!val) return [];
        return Array.isArray(val) ? val : [val];
    }

    isWidgetSelected(widgetId) {
        return this.selectedWidgetIds.includes(widgetId);
    }

    onSelectAllWidgets() {
        this.props.onChange(null);
    }

    toggleWidgetChip(widgetId) {
        const current = this.selectedWidgetIds;
        if (current.includes(widgetId)) {
            const next = current.filter((id) => id !== widgetId);
            this.props.onChange(next.length > 0 ? next : null);
        } else {
            this.props.onChange([...current, widgetId]);
        }
    }
}
