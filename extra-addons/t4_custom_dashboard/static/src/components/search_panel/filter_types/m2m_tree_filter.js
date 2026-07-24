/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";

const filterRegistry = registry.category("t4_dashboard_filters");

export class M2mTreeFilter extends Component {
    static type = "m2m_tree";
    static configSchema = [
        { key: "model",        type: "char", label: "Model",          required: true },
        { key: "parentField",  type: "char", label: "Parent field",   default: "parent_id" },
        { key: "displayField", type: "char", label: "Display field",  default: "complete_name" },
        { key: "nameField",    type: "char", label: "Name field",     default: "name" },
        { key: "domain",       type: "json", label: "Domain",         default: [] },
        { key: "scopeField",   type: "char", label: "Scope field" },
    ];
    static template = "t4_custom_dashboard.M2mTreeFilter";
    static props = {
        definition: { type: Object },
        value: { optional: true },
        onChange: { type: Function },
        contextValues: { type: Object },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            treeByScope: {},   // { scopeKey: [rootNodes] }
            expanded: [],
            loading: false,
        });

        onWillStart(async () => {
            await this._loadTree(this.props.contextValues);
        });
    }

    // -------------------------------------------------------------------------
    // Data loading
    // -------------------------------------------------------------------------

    _getScopeKey(contextValues) {
        const cfg = this.props.definition.config || {};
        if (!cfg.scopeField) return "__all__";
        const val = contextValues[cfg.scopeField];
        if (!val) return "__all__";
        return Array.isArray(val) ? val.slice().sort().join(",") : String(val);
    }

    async _loadTree(contextValues) {
        const cfg = this.props.definition.config || {};
        const model = cfg.model;
        if (!model) return;

        const scopeKey = this._getScopeKey(contextValues);
        if (this.state.treeByScope[scopeKey]) return; // already loaded

        this.state.loading = true;
        try {
            let domain = cfg.domain ? [...cfg.domain] : [];

            if (cfg.scopeField === "company_id") {
                const companyIds = contextValues[cfg.scopeField];
                if (companyIds && companyIds.length > 0) {
                    domain = [...domain, ["company_id", "in", companyIds]];
                } else {
                    const currentId = user.context && user.context.allowed_company_ids
                        ? user.context.allowed_company_ids[0]
                        : null;
                    if (currentId) {
                        domain = [...domain, ["company_id", "=", currentId]];
                    }
                }
            } else if (cfg.scopeField) {
                const scopeVal = contextValues[cfg.scopeField];
                if (scopeVal) {
                    const ids = Array.isArray(scopeVal) ? scopeVal : [scopeVal];
                    domain = [...domain, [cfg.scopeField, "in", ids]];
                }
            }

            const parentField = cfg.parentField || "parent_id";
            const displayField = cfg.displayField || "complete_name";
            const nameField = cfg.nameField || "name";
            const fields = ["id", nameField, displayField, parentField];

            const records = await this.orm.searchRead(
                model,
                domain,
                fields,
                { order: displayField + " asc", limit: 5000 }
            );

            const tree = this._buildTree(records, parentField, nameField, displayField);
            this.state.treeByScope[scopeKey] = tree;
        } catch (e) {
            console.error("M2mTreeFilter: error loading tree", e);
            this.state.treeByScope[scopeKey] = [];
        } finally {
            this.state.loading = false;
        }
    }

    _buildTree(records, parentField, nameField, displayField) {
        const map = {};
        const roots = [];

        for (const r of records) {
            map[r.id] = {
                id: r.id,
                name: r[nameField] || r[displayField] || String(r.id),
                displayName: r[displayField] || r[nameField] || String(r.id),
                parentId: r[parentField] ? (Array.isArray(r[parentField]) ? r[parentField][0] : r[parentField]) : null,
                children: [],
            };
        }

        for (const node of Object.values(map)) {
            if (node.parentId && map[node.parentId]) {
                map[node.parentId].children.push(node);
            } else {
                roots.push(node);
            }
        }

        return roots;
    }

    // -------------------------------------------------------------------------
    // Selection helpers
    // -------------------------------------------------------------------------

    get selectedIds() {
        const val = this.props.value;
        if (!val) return [];
        return Array.isArray(val) ? val : [val];
    }

    get currentTree() {
        const key = this._getScopeKey(this.props.contextValues);
        return this.state.treeByScope[key] || [];
    }

    isSelected(id) {
        return this.selectedIds.includes(id);
    }

    isExpanded(id) {
        return this.state.expanded.includes(id);
    }

    toggleExpand(id) {
        const idx = this.state.expanded.indexOf(id);
        if (idx > -1) {
            this.state.expanded.splice(idx, 1);
        } else {
            this.state.expanded.push(id);
        }
    }

    _collectDescendantIds(node) {
        const ids = [node.id];
        for (const child of (node.children || [])) {
            ids.push(...this._collectDescendantIds(child));
        }
        return ids;
    }

    _findNode(tree, id) {
        for (const node of tree) {
            if (node.id === id) return node;
            const found = this._findNode(node.children || [], id);
            if (found) return found;
        }
        return null;
    }

    toggleNode(id) {
        const node = this._findNode(this.currentTree, id);
        if (!node) return;

        const allIds = this._collectDescendantIds(node);
        const current = this.selectedIds;
        const isSelected = current.includes(id);

        let next;
        if (isSelected) {
            // deselect self + all descendants
            next = current.filter((x) => !allIds.includes(x));
        } else {
            // select self + all descendants
            next = [...new Set([...current, ...allIds])];
        }

        this.props.onChange(next.length > 0 ? next : null);
    }

    clearAll() {
        this.props.onChange(null);
    }

    selectAll() {
        const collectAll = (nodes) => {
            let ids = [];
            for (const n of nodes) {
                ids.push(n.id);
                ids = ids.concat(collectAll(n.children || []));
            }
            return ids;
        };
        const allIds = collectAll(this.currentTree);
        this.props.onChange(allIds.length > 0 ? allIds : null);
    }
}

filterRegistry.add("m2m_tree", {
    component: M2mTreeFilter,
    getInitialValue: (definition) => definition.default ?? null,
});
