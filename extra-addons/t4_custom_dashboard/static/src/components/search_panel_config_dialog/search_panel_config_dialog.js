/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { FilterEditModal } from "./filter_edit_modal";

/**
 * SearchPanelConfigDialog — admin UI for managing a dashboard's filter list.
 *
 * Props:
 *   initialConfig  — Array of filter definitions (may be null/empty for v1 dashboards)
 *   widgets        — Array of widget objects from dashboard state
 *   onSave         — (newFilterArray) => void
 *   onClose        — () => void
 */
export class SearchPanelConfigDialog extends Component {
    static template = "t4_custom_dashboard.SearchPanelConfigDialog";
    static components = { FilterEditModal };
    static props = {
        initialConfig: { optional: true },
        widgets: { type: Array },
        onSave: { type: Function },
        onClose: { type: Function },
    };

    setup() {
        const initial = Array.isArray(this.props.initialConfig) ? this.props.initialConfig : [];

        this.state = useState({
            activeTab: "visual",
            filters: initial.map((f) => ({ ...f })),
            jsonText: JSON.stringify(initial, null, 2),
            jsonError: null,
            showEditModal: false,
            editingIndex: null,         // null = adding new
            dragSrcIndex: null,
        });

        // snapshot for dirty check
        this._initialJson = JSON.stringify(initial);
    }

    // -------------------------------------------------------------------------
    // Tab management
    // -------------------------------------------------------------------------

    switchTab(tab) {
        if (tab === this.state.activeTab) return;

        if (this.state.activeTab === "json") {
            // Switching away from JSON: validate first
            if (!this._parseAndApplyJson()) return;
        }

        if (tab === "json") {
            // Regenerate JSON from current visual state
            this.state.jsonText = JSON.stringify(this.state.filters, null, 2);
            this.state.jsonError = null;
        }

        this.state.activeTab = tab;
    }

    _parseAndApplyJson() {
        const raw = this.state.jsonText || "";
        try {
            const parsed = JSON.parse(raw);
            if (!Array.isArray(parsed)) {
                this.state.jsonError = "JSON phải là một mảng (array) các filter";
                return false;
            }
            this.state.filters = parsed;
            this.state.jsonError = null;
            return true;
        } catch (e) {
            this.state.jsonError = "JSON không hợp lệ: " + e.message;
            return false;
        }
    }

    validateJson() {
        const raw = this.state.jsonText || "";
        if (!raw.trim()) {
            this.state.jsonError = null;
            return;
        }
        try {
            const parsed = JSON.parse(raw);
            if (!Array.isArray(parsed)) {
                this.state.jsonError = "JSON phải là một mảng (array) các filter";
                return;
            }
            this.state.jsonError = null;
        } catch (e) {
            this.state.jsonError = "JSON không hợp lệ: " + e.message;
        }
    }

    // -------------------------------------------------------------------------
    // CRUD operations on filter list
    // -------------------------------------------------------------------------

    addFilter() {
        this.state.editingIndex = null;
        this.state.showEditModal = true;
    }

    editFilter(index) {
        this.state.editingIndex = index;
        this.state.showEditModal = true;
    }

    deleteFilter(index) {
        const filter = this.state.filters[index];
        if (!window.confirm(`Xóa filter "${filter.label || filter.id}"?`)) return;
        this.state.filters.splice(index, 1);
        this._syncJsonFromFilters();
    }

    onModalSave(filterDef) {
        if (this.state.editingIndex !== null) {
            this.state.filters[this.state.editingIndex] = filterDef;
        } else {
            this.state.filters.push(filterDef);
        }
        this.state.showEditModal = false;
        this.state.editingIndex = null;
        this._syncJsonFromFilters();
    }

    _syncJsonFromFilters() {
        if (this.state.activeTab === "json") {
            this.state.jsonText = JSON.stringify(this.state.filters, null, 2);
        }
    }

    // -------------------------------------------------------------------------
    // Drag & drop reorder (HTML5 native)
    // -------------------------------------------------------------------------

    onDragStart(ev, index) {
        this.state.dragSrcIndex = index;
        ev.dataTransfer.effectAllowed = "move";
    }

    onDrop(ev, targetIndex) {
        ev.preventDefault();
        const src = this.state.dragSrcIndex;
        if (src === null || src === targetIndex) return;

        const filters = this.state.filters;
        const moved = filters.splice(src, 1)[0];
        filters.splice(targetIndex, 0, moved);
        this.state.dragSrcIndex = null;
        this._syncJsonFromFilters();
    }

    // -------------------------------------------------------------------------
    // Save / Close
    // -------------------------------------------------------------------------

    onSave() {
        if (this.state.activeTab === "json") {
            if (!this._parseAndApplyJson()) return;
        }
        if (this.state.jsonError) return;
        this.props.onSave(this.state.filters);
    }

    onClose() {
        const currentJson = JSON.stringify(this.state.filters);
        if (currentJson !== this._initialJson) {
            if (!window.confirm("Có thay đổi chưa được lưu. Bạn có muốn thoát không?")) return;
        }
        this.props.onClose();
    }

    // -------------------------------------------------------------------------
    // Getters
    // -------------------------------------------------------------------------

    get editingFilter() {
        if (this.state.editingIndex !== null) {
            return this.state.filters[this.state.editingIndex];
        }
        return {};
    }
}
