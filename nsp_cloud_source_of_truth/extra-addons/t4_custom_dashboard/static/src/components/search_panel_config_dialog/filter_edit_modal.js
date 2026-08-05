/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { commonSchema } from "../search_panel/filter_types/base_filter";
// Ensure all 5 built-in filter types are self-registered before the dialog mounts
import "../search_panel/filter_types/index";
import { DynamicFieldInput } from "./dynamic_field_input";

const filterRegistry = registry.category("t4_dashboard_filters");

/**
 * FilterEditModal — form dialog for creating or editing a single filter definition.
 *
 * Props:
 *   initial  — existing filter object (empty {} for new)
 *   widgets  — Array of widget objects from dashboard state
 *   onSave   — (filterDef) => void
 *   onClose  — () => void
 */
export class FilterEditModal extends Component {
    static template = "t4_custom_dashboard.FilterEditModal";
    static components = { DynamicFieldInput };
    static props = {
        initial: { type: Object },
        widgets: { type: Array },
        onSave: { type: Function },
        onClose: { type: Function },
    };

    setup() {
        // Flatten initial into editable state values
        const init = this.props.initial || {};
        const initConfig = init.config || {};

        this.state = useState({
            values: {
                id: init.id || "",
                label: init.label || "",
                field: init.field || "",
                appliesTo: init.appliesTo !== undefined ? init.appliesTo : null,
                default: init.default !== undefined ? init.default : null,
                type: init.type || "company",
                // config keys merged in flat
                ...initConfig,
            },
            errors: {},
        });
    }

    // -------------------------------------------------------------------------
    // Schema helpers
    // -------------------------------------------------------------------------

    get availableTypes() {
        return filterRegistry.getEntries().map(([key]) => key);
    }

    get currentTypeSchema() {
        const entry = filterRegistry.get(this.state.values.type);
        if (!entry || !entry.component) return [];
        return entry.component.configSchema || [];
    }

    get allSchemaFields() {
        return [...commonSchema, ...this.currentTypeSchema];
    }

    // -------------------------------------------------------------------------
    // Field value management
    // -------------------------------------------------------------------------

    getFieldValue(key) {
        return this.state.values[key];
    }

    setFieldValue(key, value) {
        this.state.values[key] = value;
        // Clear error on change
        if (this.state.errors[key]) {
            delete this.state.errors[key];
        }
    }

    onTypeChange(ev) {
        const newType = ev.target.value;
        const oldSchema = this.currentTypeSchema;
        const newEntry = filterRegistry.get(newType);
        const newSchema = (newEntry && newEntry.component && newEntry.component.configSchema) || [];

        // Drop config keys that belong to old type but not common or new type
        const commonKeys = new Set(commonSchema.map((s) => s.key));
        const newConfigKeys = new Set(newSchema.map((s) => s.key));
        for (const field of oldSchema) {
            if (!commonKeys.has(field.key) && !newConfigKeys.has(field.key)) {
                delete this.state.values[field.key];
            }
        }

        // Set defaults for new config keys if not already set
        for (const field of newSchema) {
            if (!(field.key in this.state.values) && field.default !== undefined) {
                this.state.values[field.key] = field.default;
            }
        }

        this.state.values.type = newType;
        this.state.errors = {};
    }

    // -------------------------------------------------------------------------
    // Validation & Save
    // -------------------------------------------------------------------------

    validate() {
        const errors = {};
        for (const field of this.allSchemaFields) {
            if (field.key === "default" || field.key === "appliesTo") continue;
            const val = this.state.values[field.key];
            const isEmpty = val === null || val === undefined || val === "";
            if (field.required && isEmpty) {
                errors[field.key] = `"${field.label || field.key}" là bắt buộc`;
            } else if (!isEmpty && field.validator) {
                const result = field.validator(val);
                if (result !== true) {
                    errors[field.key] = result;
                }
            }
        }
        return errors;
    }

    onSave() {
        const errors = this.validate();
        if (Object.keys(errors).length > 0) {
            this.state.errors = errors;
            return;
        }

        // Build clean filter definition
        const commonKeys = new Set(commonSchema.map((s) => s.key));
        const configObj = {};
        for (const field of this.currentTypeSchema) {
            const val = this.state.values[field.key];
            if (val !== undefined && val !== null && val !== "") {
                configObj[field.key] = val;
            }
        }

        const filterDef = {
            id: this.state.values.id,
            type: this.state.values.type,
            label: this.state.values.label,
            field: this.state.values.field || null,
            default: this.state.values.default !== undefined ? this.state.values.default : null,
            appliesTo: this.state.values.appliesTo !== undefined ? this.state.values.appliesTo : null,
            config: configObj,
        };

        this.props.onSave(filterDef);
    }
}
