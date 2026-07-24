/** @odoo-module **/

import { Component, useState, useRef, onWillStart, onWillUpdateProps, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { registry } from "@web/core/registry";

const filterRegistry = registry.category("t4_dashboard_filters");

export class M2mFilter extends Component {
    static type = "m2m";
    static configSchema = [
        { key: "model",        type: "char", label: "Model",         required: true, placeholder: "e.g. product.product" },
        { key: "displayField", type: "char", label: "Display field", default: "display_name" },
        { key: "domain",       type: "json", label: "Domain Odoo",   default: [] },
        { key: "scopeField",   type: "char", label: "Scope field" },
    ];
    static template = "t4_custom_dashboard.M2mFilter";
    static props = {
        definition: { type: Object },
        value: { optional: true },
        onChange: { type: Function },
        contextValues: { type: Object },
    };

    setup() {
        this.orm = useService("orm");
        this.selectRef = useRef("select");
        this.state = useState({
            options: [],
            loading: false,
            error: null,
        });
        this._select2Initialized = false;

        onWillStart(async () => {
            await this._loadOptions(this.props);
        });

        onMounted(() => {
            this._initSelect2();
        });

        onWillUpdateProps(async (nextProps) => {
            const cfg = this.props.definition.config || {};
            if (cfg.scopeField) {
                const oldScope = this.props.contextValues[cfg.scopeField];
                const newScope = nextProps.contextValues[cfg.scopeField];
                if (JSON.stringify(oldScope) !== JSON.stringify(newScope)) {
                    await this._loadOptions(nextProps);
                    // Sau khi state.options thay đổi, OWL re-render → cần re-init select2
                    // sau khi DOM patched. Dùng microtask để chờ patch.
                    Promise.resolve().then(() => this._refreshSelect2());
                }
            } else if (JSON.stringify(this.props.value) !== JSON.stringify(nextProps.value)) {
                Promise.resolve().then(() => this._syncSelect2Value(nextProps.value));
            }
        });

        onWillUnmount(() => {
            this._destroySelect2();
        });
    }

    async _loadOptions(props) {
        const cfg = props.definition.config || {};
        const model = cfg.model;
        if (!model) return;

        this.state.loading = true;
        this.state.error = null;
        try {
            let domain = cfg.domain ? [...cfg.domain] : [];

            if (cfg.scopeField) {
                const scopeVal = props.contextValues[cfg.scopeField];
                if (scopeVal !== undefined && scopeVal !== null && scopeVal !== "") {
                    const ids = Array.isArray(scopeVal) ? scopeVal : [scopeVal];
                    if (ids.length > 0) {
                        domain = [...domain, [cfg.scopeField, "in", ids]];
                    }
                }
            }

            const displayField = cfg.displayField || "display_name";
            const fieldsToRead = ["id"];
            if (!fieldsToRead.includes(displayField)) fieldsToRead.push(displayField);
            if (displayField !== "name") fieldsToRead.push("name");

            // Order theo `name` (stored) — `display_name` thường non-stored
            // (vd product.product.display_name) → không order_by được.
            const records = await this.orm.searchRead(
                model,
                domain,
                fieldsToRead,
                { order: "name asc, id asc", limit: 2000 }
            );
            this.state.options = records.map((r) => ({
                id: r.id,
                label: r[displayField] || r.name || String(r.id),
            }));
        } catch (e) {
            const msg = (e && (e.data?.message || e.message)) || String(e);
            console.error(`M2mFilter[${cfg.model}] error loading options:`, e);
            this.state.error = msg;
            this.state.options = [];
        } finally {
            this.state.loading = false;
        }
    }

    // -----------------------------------------------------------------------
    // Select2 lifecycle (jQuery-based)
    // -----------------------------------------------------------------------

    _initSelect2() {
        const el = this.selectRef.el;
        if (!el || !window.$) return;

        const options = this.state.options.map((o) => ({
            id: o.id,
            text: o.label,
        }));

        $(el).select2({
            width: "100%",
            placeholder: "Chọn...",
            allowClear: true,
            multiple: true,
            data: options,
        });

        $(el).on("change.t4filter", () => {
            const vals = $(el).val() || [];
            const ids = vals.map((v) => parseInt(v)).filter((n) => !isNaN(n));
            this.props.onChange(ids.length > 0 ? ids : null);
        });

        this._syncSelect2Value(this.props.value);
        this._select2Initialized = true;
    }

    _refreshSelect2() {
        const el = this.selectRef.el;
        if (!el || !window.$) return;

        // Destroy and re-init với data mới (select2 không có API "set data" sạch)
        this._destroySelect2();
        // Đảm bảo el vẫn còn (component chưa unmount)
        if (this.selectRef.el) {
            this._initSelect2();
        }
    }

    _syncSelect2Value(value) {
        const el = this.selectRef.el;
        if (!el || !window.$ || !this._select2Initialized) return;
        const ids = Array.isArray(value) ? value : (value ? [value] : []);
        const stringIds = ids.map(String);
        $(el).val(stringIds).trigger("change.select2");
    }

    _destroySelect2() {
        const el = this.selectRef.el;
        if (!el || !window.$) return;
        try {
            $(el).off("change.t4filter");
            if ($(el).data("select2")) {
                $(el).select2("destroy");
            }
        } catch (e) {
            // ignore destroy errors
        }
        this._select2Initialized = false;
    }
}

filterRegistry.add("m2m", {
    component: M2mFilter,
    getInitialValue: (definition) => definition.default ?? null,
});
