/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount, onPatched, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { loadBundle } from "@web/core/assets";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { user } from "@web/core/user";
import { _t } from "@web/core/l10n/translation";

// Import Components con
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";
import { StatsSummary } from "../components/stats_summary/stats_summary";
import { StatAction } from "../components/stat_action/stat_action";
import { KanbanEmbed } from "../components/kanban_embed/kanban_embed";
import { ChartComponent } from "../components/chart_component/chart_component";
import { WidgetConfigDialog } from "../components/widget_config_dialog/widget_config_dialog";
import { SearchPanel, computeInitialFilterValues } from "../components/search_panel/search_panel";
import { DashboardSelector } from "../components/dashboard_selector/dashboard_selector";
import { SaveDashboardDialog } from "../components/save_dashboard_dialog/save_dashboard_dialog";
import { ImportExportDialog } from "../components/import_export_dialog/import_export_dialog";
import { SearchPanelConfigDialog } from "../components/search_panel_config_dialog/search_panel_config_dialog";
import { HelpTooltip } from "../components/help_tooltip/help_tooltip";

// Import Hooks & Utils
import { ChartUtils } from "../utils/chart_utils";
import { useDashboardManager } from "../hooks/use_dashboard_manager";
import { useAutoRefresh } from "../hooks/use_auto_refresh";

class CustomDashboard extends Component {
    static template = "t4_custom_dashboard.Dashboard";
    static components = {
        StatsSummary, StatAction, KanbanEmbed, ChartComponent, WidgetConfigDialog, SearchPanel, Dropdown,
        DropdownItem, DashboardSelector, SaveDashboardDialog, ImportExportDialog,
        SearchPanelConfigDialog, HelpTooltip,
    };

    setup() {
        this.gridRef = useRef("grid");
        this.actionService = useService("action");
        this.dialog = useService("dialog");

        this.dashboardMgr = useDashboardManager();
        this.autoRefresh = useAutoRefresh(async (silent) => {
            await this.reloadAllWidgetsData(silent);
        });

        this.notification = useService("notification");

        this.state = useState({
            editMode: false,
            widgets: [],
            searchPanelConfig: null,
            isLoading: true,
            widgetData: {},
            widgetLoading: {},
            widgetChartStates: {},
            editingWidget: null,
            showConfigDialog: false,
            showSearchPanelConfig: false,

            searchFilters: {},
            isPanelCollapsed: false,
            is_admin: false,

            // PDF export progress
            isExportingPdf: false,

            // Greeting wave: true khi 👋 đang chạy animation. Vẫy 1 lần
            // khi mount + 1 lần mỗi hover vào khối .dashboard-name. Hover
            // trong lúc đang vẫy là no-op → wave hoàn tất mới reset.
            isWaving: true,
        });

        this.grid = null;
        this.isGridInitialized = false;
        this.gridstackLoaded = false;
        this.pendingWidgets = [];
        this.loadingWidgets = new Set();
        this.filterDebounceTimer = null;
        this.user = user;

        onWillStart(async () => {
            this.state.is_admin = await user.hasGroup("base.group_system");
            await loadBundle("t4_custom_dashboard.custom_dashboard_lib");
            this.gridstackLoaded = true;

            // Đọc dashboard_xml_id từ action — menu role-specific truyền qua
            // action.params (preferred — JSON, no Python eval) HOẶC
            // action.context.dashboard_xml_id (legacy). Bỏ prefix `default_`
            // để tránh Odoo strip / conflict với view-level default_* keys.
            const action = this.props.action || {};
            const dashboardXmlId =
                action.params?.dashboard_xml_id ||
                action.context?.dashboard_xml_id ||
                action.context?.default_dashboard_xml_id ||  // backward-compat
                null;
            const { widgets, searchPanel } =
                await this.dashboardMgr.loadDefaultDashboard(dashboardXmlId);
            this.state.widgets = widgets;
            this.state.searchPanelConfig = searchPanel || null;
            // Prime filter values from config so the first widget fetch already
            // carries the configured defaults (e.g. bucket=day) instead of {}.
            this.state.searchFilters = computeInitialFilterValues(this.state.searchPanelConfig);

            this.state.isLoading = false;
            await this.loadAllWidgetData();
            this.initializeChartStates();
        });

        onMounted(() => {
            if (this.gridstackLoaded) {
                setTimeout(() => this.initGrid(), 100);
            }
        });

        onPatched(() => {
            if (this.gridstackLoaded && !this.isGridInitialized && this.gridRef.el) {
                this.initGrid();
            }
            if (this.isGridInitialized && this.pendingWidgets.length > 0) {
                setTimeout(() => this.addPendingWidgetsToGrid(), 50);
            }
        });

        onWillUnmount(() => {
            if (this.filterDebounceTimer) clearTimeout(this.filterDebounceTimer);
            this.destroyGrid();
        });
    }

    /**
     * Export toàn bộ dashboard ra PDF (client-side).
     *
     * Cơ chế:
     *   1. Lazy-load bundle `pdf_export_lib` → expose `window.html2canvas`
     *      và `window.jspdf` (UMD).
     *   2. Capture phần tử cha `.custom-dashboard` (gồm header + grid) bằng
     *      html2canvas; ép width = scrollWidth, height = scrollHeight để
     *      không bị clip widget nằm ngoài viewport.
     *   3. Tính tỉ lệ cho A4 landscape; nếu chiều cao > 1 trang → paginate
     *      bằng drawImage offset.
     *   4. Bỏ pdf.text() (jsPDF default font không hỗ trợ tiếng Việt diacritics
     *      → chữ bị mangle). Title + timestamp đã có sẵn trong header dashboard
     *      → capture chung trong canvas luôn.
     */
    async exportDashboardPdf() {
        if (this.state.isExportingPdf) return;
        // Capture trực tiếp .grid-stack (gridRef.el). html2canvas chỉ render
        // subtree của target, nên mọi thứ NGOÀI .grid-stack tự động bị loại:
        //   - dashboard-header (admin-only, greeting + buttons)
        //   - nút PDF .search-panel-pdf-btn (nằm trong SearchPanel, ngoài grid)
        // → "chừa nút in PDF ra" mà không cần ignoreElements. Tên file đã
        // chứa dashboard name + timestamp nên không cần in lại header.
        const rootEl = this.gridRef.el;
        if (!rootEl) {
            this.notification.add(_t("Không tìm thấy nội dung dashboard."), { type: "danger" });
            return;
        }

        this.state.isExportingPdf = true;
        try {
            await loadBundle("t4_custom_dashboard.pdf_export_lib");
            const html2canvas = window.html2canvas;
            const jsPDFCtor = window.jspdf?.jsPDF;
            if (!html2canvas || !jsPDFCtor) {
                throw new Error("PDF libraries failed to load");
            }

            // Chờ browser layout settle (chart redraw, gridstack pack lại)
            await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

            // Measure size từ LIVE DOM (clone iframe có thể tính sai do
            // CSS chưa apply hết). scrollHeight tính được full content kể
            // cả khi overflow hidden.
            const fullW = rootEl.scrollWidth || rootEl.offsetWidth;
            const fullH = rootEl.scrollHeight || rootEl.offsetHeight;

            // Safe cut points (px relative to rootEl top) — bottom của mỗi
            // grid-stack-item ở live DOM → pagination không cắt giữa widget.
            const rootRect = rootEl.getBoundingClientRect();
            const safeCutPx = [];
            rootEl.querySelectorAll(".grid-stack-item").forEach((w) => {
                const r = w.getBoundingClientRect();
                safeCutPx.push(Math.round(r.bottom - rootRect.top));
            });
            safeCutPx.sort((a, b) => a - b);

            const canvas = await html2canvas(rootEl, {
                backgroundColor: "#ffffff",
                scale: 2,
                useCORS: true,
                logging: false,
                width: fullW,
                height: fullH,
                windowWidth: fullW,
                windowHeight: fullH,
                scrollX: 0,
                scrollY: 0,
                ignoreElements: (el) =>
                    el.classList?.contains("grid-stack-placeholder") ||
                    el.classList?.contains("ui-resizable-handle") ||
                    el.classList?.contains("widget-controls"),
                // CLONED DOM only: add class để ẩn UI controls trong snapshot.
                // KHÔNG reflow / expand list widgets ở đây — list bị clip
                // theo widget size đúng như user thấy trên màn hình. Nếu
                // user muốn full list, có thể edit widget cao hơn rồi
                // export lại.
                onclone: (clonedDoc, clonedRoot) => {
                    if (clonedRoot?.classList) {
                        clonedRoot.classList.add("pdf-export-snapshot");
                    }
                },
            });

            // A4 landscape: 297 × 210mm. Margin 8mm.
            const pdf = new jsPDFCtor({ orientation: "landscape", unit: "mm", format: "a4", compress: true });
            const pageW = pdf.internal.pageSize.getWidth();
            const pageH = pdf.internal.pageSize.getHeight();
            const margin = 8;
            const contentW = pageW - margin * 2;
            const contentH = pageH - margin * 2;

            const ratio = contentW / canvas.width;
            const fullHmm = canvas.height * ratio;

            if (fullHmm <= contentH) {
                pdf.addImage(canvas, "PNG", margin, margin, contentW, fullHmm, undefined, "SLOW");
            } else {
                // Multi-page với SMART CUT: tránh cắt giữa widget.
                // safeCutPx (CSS px live DOM) × (canvas.height / fullH) → canvas px.
                const scaleY = canvas.height / fullH;
                const safeCutCanvasPx = safeCutPx
                    .map((p) => Math.round(p * scaleY))
                    .filter((p) => p > 0 && p <= canvas.height);

                const maxSliceHpx = Math.floor(contentH / ratio);
                const MIN_SLICE = Math.floor(maxSliceHpx * 0.3); // tránh trang quá ngắn

                let y = 0;
                let pageIndex = 0;
                while (y < canvas.height) {
                    // Tìm safe cut point LỚN NHẤT trong range (y + MIN_SLICE, y + maxSliceHpx]
                    let cut = y + maxSliceHpx;
                    for (let i = safeCutCanvasPx.length - 1; i >= 0; i--) {
                        const cp = safeCutCanvasPx[i];
                        if (cp > y + MIN_SLICE && cp <= y + maxSliceHpx) {
                            cut = cp;
                            break;
                        }
                    }
                    cut = Math.min(cut, canvas.height);
                    const h = cut - y;
                    if (h <= 0) break;

                    const tmp = document.createElement("canvas");
                    tmp.width = canvas.width;
                    tmp.height = h;
                    tmp.getContext("2d").drawImage(canvas, 0, y, canvas.width, h, 0, 0, canvas.width, h);
                    if (pageIndex > 0) pdf.addPage();
                    pdf.addImage(tmp, "PNG", margin, margin, contentW, h * ratio, undefined, "SLOW");
                    y = cut;
                    pageIndex += 1;
                }
            }

            const rawName = this.dashboardMgr.state.currentDashboardName || "dashboard";
            // ASCII-safe filename (browsers handle Unicode but tránh cho an toàn)
            const safeName = rawName.replace(/[\\/:*?"<>|]/g, "_").trim() || "dashboard";
            const now = new Date();
            const ts = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}-${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;
            pdf.save(`${safeName}-${ts}.pdf`);
            this.notification.add(_t("Đã xuất PDF thành công."), { type: "success" });
        } catch (e) {
            console.error("PDF export error:", e);
            this.notification.add(_t("Xuất PDF thất bại: ") + (e?.message || e), { type: "danger" });
        } finally {
            this.state.isExportingPdf = false;
        }
    }

    // ------------------------------------------------------------------
    // Greeting wave handlers
    // ------------------------------------------------------------------
    triggerGreetingWave() {
        // Đang vẫy → skip để không restart giữa chừng
        if (this.state.isWaving) return;
        this.state.isWaving = true;
    }

    onGreetingWaveEnd() {
        this.state.isWaving = false;
    }

    async handleSaveDashboard(id, name, desc) {
        const success = await this.dashboardMgr.saveDashboard(
            id, name, desc, this.state.widgets, this.state.searchPanelConfig
        );
        if (success) this.dashboardMgr.closeSaveDialog();
    }

    async handleSwitchDashboard(id) {
        const result = await this.dashboardMgr.switchDashboard(id);
        if (result !== null) {
            this.destroyGrid();
            this.state.widgets = result.widgets;
            this.state.searchPanelConfig = result.searchPanel || null;
            this.state.searchFilters = computeInitialFilterValues(this.state.searchPanelConfig);
            this.pendingWidgets = [];

            await this.loadAllWidgetData();
            this.initializeChartStates();
            setTimeout(() => this.initGrid(), 100);
        }
        this.dashboardMgr.closeDashboardDialog();
    }

    async handleDeleteDashboard(id) {
        const isCurrent = (id === this.dashboardMgr.state.currentDashboardId);
        const success = await this.dashboardMgr.deleteDashboard(id);
        if (success && isCurrent) {
            const { widgets, searchPanel } = await this.dashboardMgr.loadDefaultDashboard();
            this.destroyGrid();
            this.state.widgets = widgets;
            this.state.searchPanelConfig = searchPanel || null;
            this.state.searchFilters = computeInitialFilterValues(this.state.searchPanelConfig);
            await this.loadAllWidgetData();
            this.initializeChartStates();
            setTimeout(() => this.initGrid(), 100);
        }
    }

    async loadAllWidgetData() {
        this.autoRefresh.manualRefreshTrigger();
        const promises = this.state.widgets.map((widget) => this._fetchOneWidget(widget.id));
        await Promise.all(promises);
    }

    async reloadAllWidgetsData(silent = false) {
        const promises = this.state.widgets.map((widget) => this._fetchOneWidget(widget.id, silent));
        await Promise.all(promises);

        this.state.widgetData = { ...this.state.widgetData };
        if (!silent) this.state.widgetLoading = { ...this.state.widgetLoading };
    }

    async _fetchOneWidget(widgetId, silent = false) {
        const widget = this.state.widgets.find((w) => w.id === widgetId);
        if (!widget || !widget.dataSource || this.loadingWidgets.has(widgetId)) return;

        this.loadingWidgets.add(widgetId);
        if (!silent) this.state.widgetLoading[widgetId] = true;

        try {
            // 🆕 Chỉ gửi topLimit nếu widget có enableTopLimit = true
            const topLimit = widget.type === 'chart' && widget.data.enableTopLimit
                ? (this.state.widgetChartStates[widgetId]?.topLimit || null)
                : null;

            const filters = {
                ...this.state.searchFilters,
                topLimit: topLimit
            };

            const config = {
                id: widget.id,
                type: widget.type,
                data_source: widget.dataSource,
                filters: filters,
                search_panel: this.state.searchPanelConfig || [],
            };
            const data = await rpc("/t4_custom_dashboard/get_widget_data", { widget_config: config });
            this.state.widgetData[widgetId] = data.error ? { error: data.error } : data;
        } catch (error) {
            this.state.widgetData[widgetId] = { error: error.message };
        } finally {
            this.loadingWidgets.delete(widgetId);
            if (!silent) this.state.widgetLoading[widgetId] = false;
        }
    }
    async onFiltersChange(filters) {
        this.state.searchFilters = { ...filters };
        if (this.filterDebounceTimer) clearTimeout(this.filterDebounceTimer);
        this.filterDebounceTimer = setTimeout(async () => {
            await this.reloadAllWidgetsData();
        }, 500);
    }

    initializeChartStates() {
        this.state.widgets.forEach((widget) => {
            if (widget.type === "chart" && !this.state.widgetChartStates[widget.id]) {
                this.state.widgetChartStates[widget.id] = {
                    currentType: widget.data.chartType || "bar",
                    sortOrder: null,
                    isStacked: true,
                    topLimit: widget.data.enableTopLimit ? (widget.data.topLimit || 10) : null // 🆕 Chỉ set nếu enable
                };
            }
        });
    }

    getWidgetDisplayData(widget) {
        return ChartUtils.processWidgetDisplayData(
            widget,
            this.state.widgetData[widget.id],
            this.state.widgetChartStates[widget.id]
        );
    }

    // Embed widgets nhan filter qua context.embed_filters — CHI inject
    // filter co appliesTo (mang) chua dung widget.id (khong dung null).
    getEmbedConfig(widget) {
        const embed = widget.embed;
        if (!embed) return embed;
        const cfg = this.state.searchPanelConfig || [];
        const ef = {};
        for (const f of cfg) {
            const at = f.appliesTo;
            if (!Array.isArray(at) || !at.includes(widget.id)) continue;
            const val = this.state.searchFilters?.[f.id];
            if (val === undefined || val === null || val === "") continue;
            if (Array.isArray(val) && val.length === 0) continue;
            ef[f.id] = { type: f.type, field: f.field, value: val };
        }
        if (Object.keys(ef).length === 0) return embed;
        return { ...embed, context: { ...(embed.context || {}), embed_filters: JSON.stringify(ef) } };
    }

    embedKey(widget) {
        const cfg = this.getEmbedConfig(widget);
        const ef = cfg && cfg.context && cfg.context.embed_filters;
        return widget.id + "|" + (ef ? JSON.stringify(ef) : "");
    }

    /**
     * Responsive cellHeight + column theo viewport.
     * - >=1200px: desktop 12 cột, cellHeight 80px
     * - 768-1199px: tablet 12 cột, cellHeight 65px
     * - <768px: mobile 1 cột stack vertically, cellHeight 70px
     */
    _getResponsiveGridOpts() {
        const w = window.innerWidth || 1200;
        if (w < 768) {
            return { column: 1, cellHeight: 70 };
        } else if (w < 1200) {
            return { column: 12, cellHeight: 65 };
        }
        return { column: 12, cellHeight: 80 };
    }

    initGrid() {
        if (!this.gridRef.el || this.isGridInitialized || !window.GridStack) return;
        // Chống race khi switch dashboard: destroyGrid() dùng removeAll(false)
        // nên DOM còn giữ item CŨ; OWL re-render item mới là bất đồng bộ (sau).
        // Nếu initGrid (qua onPatched/setTimeout) chạy trước khi DOM có đủ item
        // mới, GridStack.init sẽ đăng ký nhầm item cũ rồi khóa isGridInitialized
        // → item mới không được lay out (dồn về góc, position:absolute). Chờ tới
        // patch mà số .grid-stack-item khớp số widget rồi mới init.
        const domItems = this.gridRef.el.querySelectorAll(".grid-stack-item").length;
        if (domItems !== this.state.widgets.length) {
            return;
        }
        try {
            const responsive = this._getResponsiveGridOpts();
            this.grid = window.GridStack.init({
                column: responsive.column,
                cellHeight: responsive.cellHeight,
                margin: 10,
                float: false,
                removable: false,
                animate: true,
                resizable: { handles: "e, se, s, sw, w" },
                draggable: { handle: ".grid-stack-item-content", scroll: true },
            }, this.gridRef.el);

            if (this.grid) {
                this.isGridInitialized = true;
                if (!this.state.editMode) this.grid.disable();

                this.grid.on("change", (event, items) => {
                    if (items && this.state.editMode) {
                        items.forEach((item) => {
                            const widget = this.state.widgets.find((w) => w.id === item.id);
                            if (widget) {
                                widget.x = item.x; widget.y = item.y;
                                widget.w = item.w; widget.h = item.h;
                            }
                        });
                    }
                });

                // Debounced resize handler — apply lại cellHeight + column khi
                // user resize viewport (desktop ↔ tablet ↔ mobile).
                this._resizeHandler = () => {
                    if (this._resizeTimer) clearTimeout(this._resizeTimer);
                    this._resizeTimer = setTimeout(() => {
                        if (!this.grid) return;
                        const r = this._getResponsiveGridOpts();
                        try {
                            this.grid.cellHeight(r.cellHeight);
                            this.grid.column(r.column);
                        } catch (e) { console.error("Grid responsive resize error:", e); }
                    }, 200);
                };
                window.addEventListener("resize", this._resizeHandler);
            }
        } catch (e) { console.error("Grid init error:", e); }
    }

    destroyGrid() {
        if (this._resizeHandler) {
            window.removeEventListener("resize", this._resizeHandler);
            this._resizeHandler = null;
        }
        if (this._resizeTimer) {
            clearTimeout(this._resizeTimer);
            this._resizeTimer = null;
        }
        if (this.grid) {
            try { this.grid.destroy(false); } catch (e) { }
            this.grid = null;
            this.isGridInitialized = false;
        }
    }

    toggleEditMode() {
        this.state.editMode = !this.state.editMode;
        if (this.grid) {
            if (this.state.editMode) {
                this.grid.enable();
                this.autoRefresh.stop();
            } else {
                this.grid.disable();
                if (this.dashboardMgr.state.currentDashboardId) {
                    this.dashboardMgr.saveDashboard(
                        this.dashboardMgr.state.currentDashboardId,
                        this.dashboardMgr.state.currentDashboardName,
                        "",
                        this.state.widgets,
                        this.state.searchPanelConfig
                    );
                }
                if (this.autoRefresh.state.enabled) this.autoRefresh.start();
            }
        }
    }

    removeWidget(widgetId) {
        const el = this.gridRef.el?.querySelector(`[gs-id="${widgetId}"]`);
        if (el && this.grid) this.grid.removeWidget(el, true);
        const index = this.state.widgets.findIndex((w) => w.id === widgetId);
        if (index !== -1) {
            this.state.widgets.splice(index, 1);
            delete this.state.widgetData[widgetId];
            delete this.state.widgetChartStates[widgetId];
        }
    }

    async saveWidgetConfig(widgetConfig) {
        if (this.state.editingWidget) {
            const index = this.state.widgets.findIndex((w) => w.id === this.state.editingWidget.id);
            if (index !== -1) {
                this.state.widgets[index] = widgetConfig;
                await this._fetchOneWidget(widgetConfig.id);
            }
        } else {
            this.pendingWidgets.push(widgetConfig.id);
            this.state.widgets.push(widgetConfig);
            await this._fetchOneWidget(widgetConfig.id);
            if (widgetConfig.type === "chart") {
                this.state.widgetChartStates[widgetConfig.id] = {
                    currentType: widgetConfig.data.chartType || "bar",
                    sortOrder: null,
                    isStacked: true,
                    topLimit: widgetConfig.data.enableTopLimit ? (widgetConfig.data.topLimit || 10) : null // 🆕
                };
            }
        }

        if (this.dashboardMgr.state.currentDashboardId) {
            this.dashboardMgr.saveDashboard(
                this.dashboardMgr.state.currentDashboardId,
                this.dashboardMgr.state.currentDashboardName,
                "",
                this.state.widgets,
                this.state.searchPanelConfig
            );
        }
        this.closeConfigDialog();
    }

    addPendingWidgetsToGrid() {
        if (!this.grid || !this.pendingWidgets.length) return;
        this.pendingWidgets.forEach((id) => {
            const el = this.gridRef.el?.querySelector(`[gs-id="${id}"]`);
            if (el) this.grid.makeWidget(el);
        });
        this.pendingWidgets = [];
    }

    onPanelToggle(isCollapsed) {
        this.state.isPanelCollapsed = isCollapsed;
        if (this.grid) setTimeout(() => this.grid.compact(), 300);
    }

    openAddWidgetDialog() { this.state.showConfigDialog = true; this.state.editingWidget = null; }
    openEditWidgetDialog(widget) { this.state.showConfigDialog = true; this.state.editingWidget = widget; }
    closeConfigDialog() { this.state.showConfigDialog = false; this.state.editingWidget = null; }

    openSearchPanelConfig() {
        this.state.showSearchPanelConfig = true;
    }

    async onSearchPanelConfigSave(newConfig) {
        this.state.searchPanelConfig = newConfig;
        this.state.searchFilters = computeInitialFilterValues(newConfig);
        this.state.showSearchPanelConfig = false;
        if (this.dashboardMgr.state.currentDashboardId) {
            await this.dashboardMgr.saveDashboard(
                this.dashboardMgr.state.currentDashboardId,
                this.dashboardMgr.state.currentDashboardName,
                "",
                this.state.widgets,
                this.state.searchPanelConfig
            );
        }
        await this.reloadAllWidgetsData();
        this.notification.add("Đã lưu cấu hình bộ lọc", { type: "success" });
    }

    // Ẩn nút điều khiển biểu đồ theo cấu hình widget.data.hiddenChartButtons
    // (mảng key: 'bar','line','pie','doughnut','sort','stack'). Mặc định hiện hết.
    isChartBtnHidden(widget, key) {
        const hidden = (widget.data && widget.data.hiddenChartButtons) || [];
        return hidden.includes(key);
    }

    onChangeChartType(wId, type) { this.state.widgetChartStates[wId].currentType = type; }
    onToggleSortOrder(wId, sort) {
        const s = this.state.widgetChartStates[wId];
        s.sortOrder = (s.sortOrder === sort) ? null : sort;
    }
    onToggleStacked(wId) { this.state.widgetChartStates[wId].isStacked = !this.state.widgetChartStates[wId].isStacked; }

    // Thay đổi Top Limit / Số ngày.
    // Cập nhật state NGAY mỗi keystroke (t-att-value re-render sẽ
    // hiển thị đúng giá trị user vừa gõ, không bị reset bởi auto-refresh
    // 30s hay reactive re-render khác). Refetch widget data debounce 600ms
    // để tránh gọi API liên tục khi user đang gõ nhiều ký tự.
    onChangeTopLimit(wId, limitValue) {
        const limit = limitValue ? parseInt(limitValue) : null;
        if (limit !== null && (isNaN(limit) || limit < 1)) {
            return; // Invalid input — bỏ qua nhưng không reset state
        }
        if (!this.state.widgetChartStates[wId]) {
            this.state.widgetChartStates[wId] = {};
        }
        this.state.widgetChartStates[wId].topLimit = limit;

        if (!this.topLimitDebounceTimers) {
            this.topLimitDebounceTimers = {};
        }
        if (this.topLimitDebounceTimers[wId]) {
            clearTimeout(this.topLimitDebounceTimers[wId]);
        }
        this.topLimitDebounceTimers[wId] = setTimeout(async () => {
            delete this.topLimitDebounceTimers[wId];
            await this._fetchOneWidget(wId);
        }, 600);
    }
}

registry.category("actions").add("t4_custom_dashboard.dashboard", CustomDashboard);
export default CustomDashboard;