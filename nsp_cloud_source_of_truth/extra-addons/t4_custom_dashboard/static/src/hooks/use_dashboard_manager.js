/** @odoo-module **/
import { useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

// File này quản lý danh sách dashboard và các thao tác CRUD (Lưu, Xóa, Export).
export function useDashboardManager() {
    const state = useState({
        currentDashboardId: null,
        currentDashboardName: "",
        dashboards: [],
        showDashboardDialog: false,
        showSaveDialog: false,
        showImportExportDialog: false,
        importExportMode: "export",
    });

    const loadDashboardList = async () => {
        try {
            state.dashboards = await rpc("/t4_custom_dashboard/get_dashboards", {}) || [];
        } catch (error) {
            console.error("Error loading dashboard list:", error);
        }
    };

    /**
     * Returns { widgets: Array, searchPanel: Array|null }
     */
    const loadDefaultDashboard = async (dashboardXmlId = null) => {
        try {
            const params = {};
            if (dashboardXmlId) {
                params.dashboard_xml_id = dashboardXmlId;
            }
            const result = await rpc(
                "/t4_custom_dashboard/get_default_dashboard",
                params,
            );
            await loadDashboardList();

            if (result && result.id) {
                state.currentDashboardId = result.id;
                state.currentDashboardName = result.name;
                return {
                    widgets: result.widgets || [],
                    searchPanel: result.searchPanel || null,
                };
            }
            return { widgets: [], searchPanel: null };
        } catch (error) {
            console.error("Error loading default dashboard:", error);
            return { widgets: [], searchPanel: null };
        }
    };

    /**
     * @param {number|null} id
     * @param {string} name
     * @param {string} description
     * @param {Array} widgets
     * @param {Array|null} searchPanel
     */
    const saveDashboard = async (id, name, description, widgets, searchPanel = null) => {
        try {
            const result = await rpc("/t4_custom_dashboard/save_dashboard", {
                dashboard_id: id,
                name: name,
                description: description || "",
                widgets: widgets,
                search_panel: searchPanel || null,
            });

            if (result.error) throw new Error(result.error);

            state.currentDashboardId = result.dashboard_id;
            state.currentDashboardName = result.name;
            await loadDashboardList();
            return true;
        } catch (error) {
            alert(`Error saving dashboard: ${error.message}`);
            return false;
        }
    };

    const deleteDashboard = async (id) => {
        if (!confirm("Are you sure you want to delete this dashboard?")) return false;
        try {
            const result = await rpc("/t4_custom_dashboard/delete_dashboard", { dashboard_id: id });
            if (result.error) throw new Error(result.error);
            await loadDashboardList();
            return true;
        } catch (error) {
            alert(`Error deleting: ${error.message}`);
            return false;
        }
    };

    /**
     * Returns { widgets: Array, searchPanel: Array|null } or null on error
     */
    const switchDashboard = async (id) => {
        try {
            const result = await rpc("/t4_custom_dashboard/get_dashboard", { dashboard_id: id });
            if (result.error) throw new Error(result.error);

            state.currentDashboardId = result.id;
            state.currentDashboardName = result.name;
            return {
                widgets: result.widgets || [],
                searchPanel: result.searchPanel || null,
            };
        } catch (error) {
            alert(`Error switching: ${error.message}`);
            return null;
        }
    };

    // Các tính năng phụ trợ
    const actions = {
        setDefault: async (id) => {
            await rpc("/t4_custom_dashboard/set_default", { dashboard_id: id });
            await loadDashboardList();
        },
        duplicate: async (id) => {
            const res = await rpc("/t4_custom_dashboard/duplicate_dashboard", { dashboard_id: id });
            await loadDashboardList();
            alert(`Dashboard duplicated: ${res.dashboard.name}`);
        },
        export: async () => {
            if (!state.currentDashboardId) return alert("No dashboard to export");
            const res = await rpc("/t4_custom_dashboard/export_dashboard", {
                dashboard_id: state.currentDashboardId,
            });
            const dataStr = JSON.stringify(res, null, 2);
            const url = URL.createObjectURL(new Blob([dataStr], { type: "application/json" }));
            const link = document.createElement("a");
            link.href = url;
            link.download = `dashboard_${res.name}_${Date.now()}.json`;
            link.click();
            URL.revokeObjectURL(url);
        },
        import: async (content, name) => {
            try {
                const configData = JSON.parse(content);
                const res = await rpc("/t4_custom_dashboard/import_dashboard", {
                    config_data: configData,
                    dashboard_name: name || null,
                });
                if (res.error) throw new Error(res.error);
                await loadDashboardList();
                alert(`Dashboard imported: ${res.dashboard.name}`);
                return true;
            } catch (e) {
                alert(`Error: ${e.message}`);
                return false;
            }
        },
    };

    return {
        state,
        loadDefaultDashboard,
        loadDashboardList,
        saveDashboard,
        deleteDashboard,
        switchDashboard,
        ...actions,
        // Dialog Controls
        openDashboardDialog: () => (state.showDashboardDialog = true),
        closeDashboardDialog: () => (state.showDashboardDialog = false),
        openSaveDialog: () => (state.showSaveDialog = true),
        closeSaveDialog: () => (state.showSaveDialog = false),
        openImportExportDialog: (mode) => {
            state.importExportMode = mode;
            state.showImportExportDialog = true;
        },
        closeImportExportDialog: () => (state.showImportExportDialog = false),
    };
}
