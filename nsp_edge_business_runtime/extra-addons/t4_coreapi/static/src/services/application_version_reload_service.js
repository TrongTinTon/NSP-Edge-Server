/** @odoo-module **/

import { rpcBus } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";

async function reloadApplicationForm(action, applicationId) {
    const controller = action.currentController;
    if (controller?.props?.resModel !== "core.api.application") {
        return;
    }
    if (applicationId && controller?.props?.resId !== applicationId) {
        return;
    }
    await action.restore(controller.jsId);
}

/**
 * Reload open application forms when API versions or credential state changes.
 */
export const coreApiApplicationVersionReloadService = {
    dependencies: ["bus_service", "action"],

    start(env, { bus_service, action }) {
        bus_service.subscribe("core_api_application_views_changed", async () => {
            rpcBus.trigger("CLEAR-CACHES", "get_views");
            await reloadApplicationForm(action);
        });
        bus_service.subscribe("core_api_application_reload", async ({ application_id }) => {
            await reloadApplicationForm(action, application_id);
        });
    },
};

registry
    .category("services")
    .add("t4_coreapi.application_version_reload", coreApiApplicationVersionReloadService);
