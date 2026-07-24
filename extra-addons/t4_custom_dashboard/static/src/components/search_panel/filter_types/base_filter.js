/** @odoo-module **/

import { Component } from "@odoo/owl";

/**
 * Common schema fields shared by every filter type.
 * Used by FilterEditModal to render the shared section.
 */
export const commonSchema = [
    {
        key: "id",
        type: "char",
        label: "ID (snake_case)",
        required: true,
        placeholder: "e.g. company_filter",
        validator: (v) => /^[a-z][a-z0-9_]*$/.test(v) || "ID phải bắt đầu bằng chữ cái thường, chỉ chứa a-z 0-9 _",
    },
    {
        key: "label",
        type: "char",
        label: "Nhãn hiển thị",
        required: true,
    },
    {
        key: "field",
        type: "char",
        label: "Field path Odoo (dotted)",
        placeholder: "e.g. product_id.categ_id",
    },
    {
        key: "appliesTo",
        type: "widget_picker",
        label: "Áp dụng cho widget",
    },
    {
        key: "default",
        type: "json",
        label: "Giá trị mặc định",
    },
];

/**
 * Abstract base class for all dashboard filter components.
 * Subclasses should override static type, static configSchema, and provide their own template.
 */
export class BaseFilter extends Component {
    static type = "abstract";
    static configSchema = [];
    static props = {
        definition: { type: Object },         // FilterDef object from searchPanel config
        value: { optional: true },             // current value (any shape, type-specific)
        onChange: { type: Function },          // (newValue) => void
        contextValues: { type: Object },       // {filterId: value} — all sibling filter values
    };

    /**
     * Serialize current value for RPC. Default: return value as-is.
     * Subclasses can override for custom serialization.
     */
    serialize() {
        return this.props.value;
    }
}

/**
 * Compute the initial value for a filter definition.
 * Falls back to definition.default, then null.
 * Subclasses/registry entries should override this per type.
 *
 * @param {Object} definition - FilterDef
 * @returns {*} initial value
 */
export function getInitialValue(definition) {
    const def = definition.default;
    if (def === undefined) return null;
    return def;
}
