/** @odoo-module **/

import { registry } from "@web/core/registry";
import { SelectionField, selectionField } from "@web/views/fields/selection/selection_field";

const GROUPS = [
    { label: "Wired", values: new Set(["usb", "rs232", "rs485", "ethernet", "wiegand"]) },
    { label: "Wireless", values: new Set(["bluetooth", "wifi", "cellular"]) },
];

export class NspGroupedConnectionField extends SelectionField {
    static template = "nsp_gatekeeper.GroupedConnectionField";

    get groupedChoices() {
        const options = this.options;
        return GROUPS.map((group) => ({
            label: group.label,
            choices: options
                .filter(([value]) => group.values.has(value))
                .map(([value, label]) => ({ value, label })),
        })).filter((group) => group.choices.length);
    }
}

export const nspGroupedConnectionField = {
    ...selectionField,
    component: NspGroupedConnectionField,
};

registry.category("fields").add("nsp_grouped_connection", nspGroupedConnectionField);
