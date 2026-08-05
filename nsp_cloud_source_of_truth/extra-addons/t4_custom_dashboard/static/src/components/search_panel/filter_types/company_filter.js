/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { registry } from "@web/core/registry";

const filterRegistry = registry.category("t4_dashboard_filters");

export class CompanyFilter extends Component {
    static type = "company";
    static configSchema = [];
    static template = "t4_custom_dashboard.CompanyFilter";
    static props = {
        definition: { type: Object },
        value: { optional: true },
        onChange: { type: Function },
        contextValues: { type: Object },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            companies: [],
            loading: false,
        });

        onWillStart(async () => {
            await this._loadCompanies();
        });
    }

    async _loadCompanies() {
        this.state.loading = true;
        try {
            this.state.companies = await this.orm.searchRead(
                "res.company",
                [],
                ["id", "name"],
                { order: "name asc" }
            );
        } catch (e) {
            console.error("CompanyFilter: error loading companies", e);
        } finally {
            this.state.loading = false;
        }
    }

    get selectedIds() {
        const val = this.props.value;
        if (!val) return [];
        return Array.isArray(val) ? val : [val];
    }

    isSelected(id) {
        return this.selectedIds.includes(id);
    }

    toggleCompany(id) {
        const current = this.selectedIds;
        const next = current.includes(id)
            ? current.filter((x) => x !== id)
            : [...current, id];
        this.props.onChange(next.length > 0 ? next : null);
    }
}

function getInitialValue(definition) {
    if (definition.default === "current") {
        const ids = user.context && user.context.allowed_company_ids;
        return ids && ids.length > 0 ? [ids[0]] : null;
    }
    return definition.default ?? null;
}

filterRegistry.add("company", {
    component: CompanyFilter,
    getInitialValue,
});
