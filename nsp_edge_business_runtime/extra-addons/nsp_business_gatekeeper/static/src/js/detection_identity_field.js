/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class DetectionIdentityField extends Component {
    static template = "nsp_business_gatekeeper.DetectionIdentityField";
    static props = { ...standardFieldProps };

    get identityLabel() {
        return this.props.record.data[this.props.name] || "";
    }

    get identityType() {
        const data = this.props.record.data;
        if (data.user_id) {
            return "User";
        }
        if (data.vehicle_id) {
            return "Vehicle";
        }
        return "";
    }

    get badgeClass() {
        return this.identityType === "User"
            ? "badge rounded-pill text-bg-info me-1"
            : "badge rounded-pill text-bg-success me-1";
    }
}

registry.category("fields").add("nsp_detection_identity", {
    component: DetectionIdentityField,
    supportedTypes: ["char"],
});
