/** @odoo-module **/

import { Component, useState, onWillStart, onMounted, useRef } from "@odoo/owl";

// Import filter type registry (side-effects register all built-in types)
import { filterRegistry } from "./filter_types/index";

/**
 * Compute the initial value map { filterId: value } for a search panel config.
 * Exported so the parent dashboard can prime its filter state in onWillStart,
 * eliminating the race where widgets fetch with empty filters before the panel
 * mounts and emits its own initial values.
 */
export function computeInitialFilterValues(config) {
    if (!config || !config.length) return {};
    const values = {};
    for (const filter of config) {
        const entry = filterRegistry.get(filter.type, null);
        values[filter.id] = entry
            ? entry.getInitialValue(filter)
            : (filter.default ?? null);
    }
    return values;
}

export class SearchPanel extends Component {
    static template = "t4_custom_dashboard.SearchPanel";
    static props = {
        config: { optional: true },           // Array<FilterDef> | null
        widgets: { optional: true },          // Array — not used in rendering, kept for future
        onFiltersChange: { type: Function, optional: true },
        onPanelToggle: { type: Function, optional: true },
        onExportPdf: { type: Function, optional: true },     // nút PDF trên header
        isExportingPdf: { type: Boolean, optional: true },
    };

    // Expose registry for template to query components
    get filterRegistry() {
        return filterRegistry;
    }

    setup() {
        this.panelRef = useRef("searchPanel");
        this.resizerRef = useRef("resizer");

        this.state = useState({
            isCollapsed: false,
            panelWidth: 280,
            minWidth: 200,
            maxWidth: 500,
            isResizing: false,
            values: {},       // { filterId: currentValue }
        });

        this._debounceTimer = null;

        onWillStart(() => {
            // Restore persisted panel size/state
            const savedWidth = localStorage.getItem("dashboardSearchPanelWidth");
            if (savedWidth) this.state.panelWidth = parseInt(savedWidth);

            const savedCollapsed = localStorage.getItem("dashboardSearchPanelCollapsed");
            if (savedCollapsed === "true") this.state.isCollapsed = true;

            // Initialise filter values from config
            this._initValues(this.props.config);
        });

        onMounted(() => {
            this._setupResizer();
            // Parent dashboard already primes its searchFilters from the same
            // config via computeInitialFilterValues, so no initial emit needed.
        });
    }

    // -------------------------------------------------------------------------
    // Initialisation helpers
    // -------------------------------------------------------------------------

    _initValues(config) {
        this.state.values = computeInitialFilterValues(config);
    }

    // -------------------------------------------------------------------------
    // Registry lookup (used in template via t-component)
    // -------------------------------------------------------------------------

    getRegistryEntry(type) {
        return filterRegistry.get(type, null);
    }

    // -------------------------------------------------------------------------
    // Value management
    // -------------------------------------------------------------------------

    updateValue(filterId, newVal) {
        this.state.values[filterId] = newVal;
        if (this._debounceTimer) clearTimeout(this._debounceTimer);
        this._debounceTimer = setTimeout(() => {
            this._emitFilters();
        }, 500);
    }

    _emitFilters() {
        if (this.props.onFiltersChange) {
            this.props.onFiltersChange({ ...this.state.values });
        }
    }

    // -------------------------------------------------------------------------
    // Reset
    // -------------------------------------------------------------------------

    resetFilters() {
        this._initValues(this.props.config);
        this._emitFilters();
    }

    get hasActiveFilters() {
        const config = this.props.config;
        if (!config || !config.length) return false;
        for (const filter of config) {
            const entry = filterRegistry.get(filter.type, null);
            const initial = entry
                ? entry.getInitialValue(filter)
                : (filter.default ?? null);
            const current = this.state.values[filter.id];
            if (JSON.stringify(current) !== JSON.stringify(initial)) return true;
        }
        return false;
    }

    // -------------------------------------------------------------------------
    // Panel collapse / resize
    // -------------------------------------------------------------------------

    togglePanel() {
        this.state.isCollapsed = !this.state.isCollapsed;
        localStorage.setItem("dashboardSearchPanelCollapsed", String(this.state.isCollapsed));
        // Width transition do CSS class .collapsed handle (width: 0 !important + transition 0.3s).
        // KHÔNG set panel.style.width — sẽ bị !important override và còn fight transition.
        if (this.props.onPanelToggle) {
            this.props.onPanelToggle(this.state.isCollapsed);
        }
    }

    _setupResizer() {
        const resizer = this.resizerRef.el;
        const panel = this.panelRef.el;
        if (!resizer || !panel) return;
        // Wrapper element giữ CSS var --search-panel-width (set bằng t-att-style trong template).
        // Để override mượt khi resize, ghi đè inline style trực tiếp lên wrapper.
        const wrapper = panel.closest(".search-panel-wrapper") || panel.parentElement;
        if (!wrapper) return;

        let startX = 0;
        let startWidth = 0;
        let currentWidth = 0;
        let rafId = null;
        let isDragging = false;

        const onMouseDown = (e) => {
            if (this.state.isCollapsed) return;
            isDragging = true;
            this.state.isResizing = true;
            startX = e.clientX;
            startWidth = this.state.panelWidth;
            currentWidth = startWidth;
            // Disable transition trong khi drag (đang có CSS transition: width 0.3s
            // làm mỗi update bị animate ⇒ giật).
            panel.style.transition = "none";
            document.body.style.cursor = "ew-resize";
            document.body.style.userSelect = "none";
            document.addEventListener("mousemove", onMouseMove);
            document.addEventListener("mouseup", onMouseUp);
            e.preventDefault();
        };

        const onMouseMove = (e) => {
            if (!isDragging) return;
            let newWidth = startWidth + (e.clientX - startX);
            newWidth = Math.max(this.state.minWidth, Math.min(this.state.maxWidth, newWidth));
            currentWidth = newWidth;
            // Throttle qua rAF + KHÔNG update state.panelWidth (sẽ re-render toàn component
            // + 6 filter children mỗi pixel ⇒ jank). Update CSS var trực tiếp trên wrapper:
            // nó cascade xuống panel qua `width: var(--search-panel-width)`.
            if (rafId === null) {
                rafId = requestAnimationFrame(() => {
                    rafId = null;
                    wrapper.style.setProperty("--search-panel-width", `${currentWidth}px`);
                });
            }
        };

        const onMouseUp = () => {
            if (!isDragging) return;
            isDragging = false;
            if (rafId !== null) {
                cancelAnimationFrame(rafId);
                rafId = null;
            }
            wrapper.style.setProperty("--search-panel-width", `${currentWidth}px`);
            panel.style.transition = "";
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
            // Sync state ONE LAST TIME ở cuối — chỉ re-render duy nhất 1 lần sau drag.
            this.state.panelWidth = currentWidth;
            this.state.isResizing = false;
            localStorage.setItem("dashboardSearchPanelWidth", String(currentWidth));
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
        };

        resizer.addEventListener("mousedown", onMouseDown);
    }
}
