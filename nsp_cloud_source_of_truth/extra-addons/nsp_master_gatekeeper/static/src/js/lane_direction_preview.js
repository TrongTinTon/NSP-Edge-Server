/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class NspLaneDirectionPreview extends Component {
    static template = "nsp_master_gatekeeper.LaneDirectionPreview";
    static props = { ...standardFieldProps };

    get points() {
        const relation = this.props.record?.data?.direction_line_ids;
        const records = relation?.records || [];
        return [...records]
            .sort((left, right) => {
                const leftSequence = Number(left.data?.sequence || 0);
                const rightSequence = Number(right.data?.sequence || 0);
                return leftSequence - rightSequence;
            })
            .map((record, index, ordered) => {
                const data = record.data || {};
                const nextData = ordered[index + 1]?.data || {};
                return {
                    key: record.resId || record.id || `point-${index}`,
                    number: index + 1,
                    antenna: data.antenna || this._fallbackAntenna(data),
                    identity: data.reader_identity || "",
                    connectorMs: index < ordered.length - 1
                        ? Number(nextData.duration_ms || 0)
                        : null,
                    isLast: index === ordered.length - 1,
                };
            });
    }

    _fallbackAntenna(data) {
        const reader = data.reader_id;
        const readerLabel = Array.isArray(reader) ? reader[1] : "Reader";
        const port = Number(data.port_no || 0);
        return `${readerLabel || "Reader"}-P${port || "?"}`;
    }

    formatDuration(value) {
        return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(
            Number(value || 0)
        );
    }
}

registry.category("fields").add("nsp_lane_direction_preview", {
    component: NspLaneDirectionPreview,
    supportedTypes: ["boolean"],
});
