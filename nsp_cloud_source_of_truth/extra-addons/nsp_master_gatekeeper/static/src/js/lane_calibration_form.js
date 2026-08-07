/** @odoo-module **/

import { onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { FormController } from "@web/views/form/form_controller";
import { formView } from "@web/views/form/form_view";

const AUTO_REFRESH_MS = 2000;
const LIVE_STATUSES = new Set(["ready", "running"]);

export class NspLaneCalibrationFormController extends FormController {
    setup() {
        super.setup();
        this.nspRefreshTimer = null;
        this.nspRefreshBusy = false;

        onMounted(() => {
            this.nspRefreshTimer = window.setInterval(
                () => this._nspRefreshDetectionTimeline(),
                AUTO_REFRESH_MS
            );
        });

        onWillUnmount(() => {
            if (this.nspRefreshTimer) {
                window.clearInterval(this.nspRefreshTimer);
                this.nspRefreshTimer = null;
            }
        });
    }

    async _nspRefreshDetectionTimeline() {
        const root = this.model?.root;
        if (
            this.nspRefreshBusy ||
            !root?.resId ||
            document.hidden ||
            !LIVE_STATUSES.has(root.data?.status)
        ) {
            return;
        }

        this.nspRefreshBusy = true;
        try {
            if (await root.isDirty()) {
                return;
            }
            // Reload the native form model. The One2many Detection Timeline remains
            // a standard Odoo List View; only its backing recordset is refreshed.
            await this.model.load({ resId: root.resId, resIds: root.resIds });
        } finally {
            this.nspRefreshBusy = false;
        }
    }
}

registry.category("views").add("nsp_lane_calibration_form", {
    ...formView,
    Controller: NspLaneCalibrationFormController,
});
