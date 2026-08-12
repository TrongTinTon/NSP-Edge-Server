/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

function many2oneLabel(value, fallback = "Reader") {
    if (Array.isArray(value)) {
        return value[1] || fallback;
    }
    if (value && typeof value === "object") {
        return value.display_name || value.name || fallback;
    }
    return fallback;
}

export class NspLaneSequencePreview extends Component {
    static template = "nsp_master_gatekeeper.LaneSequencePreview";
    static props = { ...standardFieldProps };

    get isParkingLane() {
        return this.props.record?.resModel === "nsp.parking.layout.lane";
    }

    get points() {
        const relation = this.isParkingLane
            ? this.props.record?.data?.antenna_sequence_ids
            : this.props.record?.data?.sequence_line_ids;
        const records = relation?.records || [];
        return [...records]
            .sort((left, right) => Number(left.data?.sequence || 0) - Number(right.data?.sequence || 0))
            .map((record, index, ordered) => {
                const data = record.data || {};
                const nextData = ordered[index + 1]?.data || {};
                const readerLabel = many2oneLabel(data.reader_id, "Reader");
                const port = Number(data.port_no || 0);
                return {
                    key: record.resId || record.id || `point-${index}`,
                    number: index + 1,
                    antenna: this.isParkingLane
                        ? `${readerLabel} / Antenna ${port || "?"}`
                        : (data.antenna || `${readerLabel}-P${port || "?"}`),
                    identity: this.isParkingLane ? "" : (data.reader_identity || ""),
                    durationText: index < ordered.length - 1
                        ? this._durationText(nextData)
                        : "",
                    isLast: index === ordered.length - 1,
                };
            });
    }

    _durationText(nextData) {
        if (this.isParkingLane) {
            const seconds = Number(nextData.duration_from_previous || 0);
            return `≤ ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(seconds)} s`;
        }
        const milliseconds = Number(nextData.duration_ms || 0);
        return `≤ ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(milliseconds)} ms`;
    }
}

registry.category("fields").add("nsp_lane_sequence_preview", {
    component: NspLaneSequencePreview,
    supportedTypes: ["boolean"],
});
