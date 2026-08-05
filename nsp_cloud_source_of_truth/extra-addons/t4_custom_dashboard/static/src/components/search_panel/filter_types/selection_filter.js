/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";

const filterRegistry = registry.category("t4_dashboard_filters");

export class SelectionFilter extends Component {
    static type = "selection";
    static configSchema = [
        { key: "options",       type: "json", label: "Options [{value,label}]", required: true },
        { key: "multi",         type: "bool", label: "Multi-select",            default: false },
    ];
    static template = "t4_custom_dashboard.SelectionFilter";
    static props = {
        definition: { type: Object },
        value: { optional: true },
        onChange: { type: Function },
        contextValues: { type: Object },
    };

    get cfg() {
        return this.props.definition.config || {};
    }

    get isMulti() {
        return !!this.cfg.multi;
    }

    get options() {
        return this.cfg.options || [];
    }

    get selectedValues() {
        const val = this.props.value;
        if (val === null || val === undefined) return [];
        if (this.isMulti) {
            return Array.isArray(val) ? val : [val];
        }
        return [val];
    }

    isSelected(optValue) {
        return this.selectedValues.includes(optValue);
    }

    toggleOption(optValue) {
        if (this.isMulti) {
            const current = this.selectedValues;
            const next = current.includes(optValue)
                ? current.filter((x) => x !== optValue)
                : [...current, optValue];
            this.props.onChange(next.length > 0 ? next : null);
        } else {
            // radio: clicking selected clears it
            const isSame = this.selectedValues[0] === optValue;
            this.props.onChange(isSame ? null : optValue);
        }
    }
}

filterRegistry.add("selection", {
    component: SelectionFilter,
    getInitialValue: (definition) => {
        // Prefer the common `default` field (edited via "Giá trị mặc định").
        // Fall back to legacy `config.defaultValue` for configs saved before
        // the duplicate field was removed from configSchema.
        if (definition.default !== undefined && definition.default !== null) {
            return definition.default;
        }
        const cfg = definition.config || {};
        if (cfg.defaultValue !== undefined) return cfg.defaultValue;
        return null;
    },
});
