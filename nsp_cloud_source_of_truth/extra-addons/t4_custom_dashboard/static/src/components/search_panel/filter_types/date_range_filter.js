/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { DateTimeInput } from "@web/core/datetime/datetime_input";
import { deserializeDate, serializeDate } from "@web/core/l10n/dates";

const filterRegistry = registry.category("t4_dashboard_filters");

const PRESET_LABELS = {
    today: "Hôm Nay",
    yesterday: "Hôm Qua",
    this_week: "Tuần Này",
    last_week: "Tuần Trước",
    this_month: "Tháng Này",
    last_month: "Tháng Trước",
    this_quarter: "Quý Này",
    last_quarter: "Quý Trước",
    this_year: "Năm Nay",
    last_year: "Năm Ngoái",
};

function formatDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function getWeekStart(date) {
    const d = new Date(date);
    const day = d.getDay();
    const diff = d.getDate() - day + (day === 0 ? -6 : 1);
    return new Date(d.setDate(diff));
}

function getQuarterStart(date) {
    const quarter = Math.floor(date.getMonth() / 3);
    return new Date(date.getFullYear(), quarter * 3, 1);
}

function resolvePreset(preset) {
    const today = new Date();
    switch (preset) {
        case "today":
            return { from: formatDate(today), to: formatDate(today) };
        case "yesterday": {
            const d = new Date(today);
            d.setDate(d.getDate() - 1);
            return { from: formatDate(d), to: formatDate(d) };
        }
        case "this_week": {
            // TRỌN tuần (Thứ 2 → Chủ nhật). Trước đây là "tuần đến hôm nay" nên
            // vào Thứ 2 nó collapse về đúng hôm nay → trùng preset "today".
            const start = getWeekStart(today);
            const end = new Date(start);
            end.setDate(end.getDate() + 6);
            return { from: formatDate(start), to: formatDate(end) };
        }
        case "last_week": {
            const start = new Date(today);
            start.setDate(start.getDate() - 7 - today.getDay());
            const end = new Date(start);
            end.setDate(end.getDate() + 6);
            return { from: formatDate(start), to: formatDate(end) };
        }
        case "this_month":
            // TRỌN tháng (mùng 1 → ngày cuối tháng).
            return {
                from: formatDate(new Date(today.getFullYear(), today.getMonth(), 1)),
                to: formatDate(new Date(today.getFullYear(), today.getMonth() + 1, 0)),
            };
        case "last_month": {
            const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
            const end = new Date(today.getFullYear(), today.getMonth(), 0);
            return { from: formatDate(start), to: formatDate(end) };
        }
        case "this_quarter": {
            // TRỌN quý.
            const start = getQuarterStart(today);
            const end = new Date(start.getFullYear(), start.getMonth() + 3, 0);
            return { from: formatDate(start), to: formatDate(end) };
        }
        case "last_quarter": {
            const start = getQuarterStart(new Date(today.getFullYear(), today.getMonth() - 3, 1));
            const end = new Date(start.getFullYear(), start.getMonth() + 3, 0);
            return { from: formatDate(start), to: formatDate(end) };
        }
        case "this_year":
            // TRỌN năm (01/01 → 31/12).
            return {
                from: formatDate(new Date(today.getFullYear(), 0, 1)),
                to: formatDate(new Date(today.getFullYear(), 11, 31)),
            };
        case "last_year":
            return {
                from: formatDate(new Date(today.getFullYear() - 1, 0, 1)),
                to: formatDate(new Date(today.getFullYear() - 1, 11, 31)),
            };
        default:
            return { from: null, to: null };
    }
}

export class DateRangeFilter extends Component {
    static type = "date_range";
    static configSchema = [
        {
            key: "presets",
            type: "json",
            label: "Presets",
            default: ["today", "this_week", "this_month", "this_quarter", "this_year"],
        },
    ];
    static template = "t4_custom_dashboard.DateRangeFilter";
    static components = { DateTimeInput };
    static props = {
        definition: { type: Object },
        value: { optional: true },       // { from: "YYYY-MM-DD"|null, to: "YYYY-MM-DD"|null }
        onChange: { type: Function },
        contextValues: { type: Object },
    };

    // ---- Luxon helpers (DateTimeInput dùng luxon DateTime ↔ string YYYY-MM-DD) ----
    get fromDate() {
        return this.currentValue.from ? deserializeDate(this.currentValue.from) : false;
    }

    get toDate() {
        return this.currentValue.to ? deserializeDate(this.currentValue.to) : false;
    }

    onFromDateChange(d) {
        this.props.onChange({
            ...this.currentValue,
            from: d ? serializeDate(d) : null,
        });
    }

    onToDateChange(d) {
        this.props.onChange({
            ...this.currentValue,
            to: d ? serializeDate(d) : null,
        });
    }

    get currentValue() {
        return this.props.value || { from: null, to: null };
    }

    get activePreset() {
        const val = this.currentValue;
        if (!val.from && !val.to) return null;
        const cfg = this.props.definition.config || {};
        const presets = cfg.presets || ["today", "this_week", "this_month", "this_quarter", "this_year"];
        for (const p of presets) {
            const resolved = resolvePreset(p);
            if (resolved.from === val.from && resolved.to === val.to) return p;
        }
        return null;
    }

    get availablePresets() {
        const cfg = this.props.definition.config || {};
        const keys = cfg.presets || ["today", "this_week", "this_month", "this_quarter", "this_year"];
        return keys.map((k) => ({ key: k, label: PRESET_LABELS[k] || k }));
    }

    selectPreset(preset) {
        const resolved = resolvePreset(preset);
        this.props.onChange(resolved);
    }

    clearPreset() {
        if (this.activePreset) {
            this.props.onChange({ from: null, to: null });
        }
    }

    onFromChange(e) {
        this.props.onChange({ ...this.currentValue, from: e.target.value || null });
    }

    onToChange(e) {
        this.props.onChange({ ...this.currentValue, to: e.target.value || null });
    }

    clearAll() {
        this.props.onChange({ from: null, to: null });
    }

    get hasValue() {
        const val = this.currentValue;
        return !!(val.from || val.to);
    }
}

filterRegistry.add("date_range", {
    component: DateRangeFilter,
    getInitialValue: (definition) => {
        const def = definition.default;
        if (!def) return { from: null, to: null };
        if (typeof def === "string") return resolvePreset(def);
        return def;
    },
});
