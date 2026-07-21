/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class NspMeasurementLive extends Component {
    static template = xml`
        <div class="nsp-measurement-live">
            <div class="nsp-measurement-live__header">
                <div>
                    <h2><t t-esc="state.measurementCode || 'Live Measurement'"/></h2>
                    <div class="text-muted">
                        Controller: <t t-esc="state.controllerCode || '-'"/>
                    </div>
                </div>
                <div class="nsp-measurement-live__status">
                    <span class="badge text-bg-info"><t t-esc="state.status || 'loading'"/></span>
                    <strong><t t-esc="state.eventCount"/> events</strong>
                </div>
            </div>

            <div class="nsp-measurement-live__error alert alert-danger" t-if="state.error">
                <t t-esc="state.error"/>
            </div>

            <div class="nsp-measurement-live__summary">
                <table class="table table-sm table-hover">
                    <thead>
                        <tr>
                            <th>Reader Serial</th><th>Antenna</th><th>Reads</th>
                            <th>Min RSSI</th><th>Average RSSI</th><th>Max RSSI</th>
                            <th>First Seen</th><th>Last Seen</th>
                        </tr>
                    </thead>
                    <tbody>
                        <t t-foreach="state.summary" t-as="row" t-key="row.serial_number + '-' + row.antenna_no">
                            <tr>
                                <td><t t-esc="row.serial_number"/></td>
                                <td><t t-esc="row.antenna_no"/></td>
                                <td><t t-esc="row.read_count"/></td>
                                <td><t t-esc="formatRssi(row.min_rssi_dbm)"/></td>
                                <td><t t-esc="formatRssi(row.average_rssi_dbm)"/></td>
                                <td><t t-esc="formatRssi(row.max_rssi_dbm)"/></td>
                                <td><t t-esc="row.first_read_at || '-'"/></td>
                                <td><t t-esc="row.last_read_at || '-'"/></td>
                            </tr>
                        </t>
                        <tr t-if="!state.summary.length">
                            <td colspan="8" class="text-center text-muted">Waiting for measurement data...</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <h4>Latest Events</h4>
            <div class="nsp-measurement-live__events">
                <table class="table table-sm table-striped">
                    <thead>
                        <tr><th>Read At</th><th>Reader</th><th>Antenna</th><th>TID</th><th>RSSI</th></tr>
                    </thead>
                    <tbody>
                        <t t-foreach="state.events" t-as="event" t-key="event.id">
                            <tr>
                                <td><t t-esc="event.read_at"/></td>
                                <td><t t-esc="event.serial_number"/></td>
                                <td><t t-esc="event.antenna_no"/></td>
                                <td class="font-monospace"><t t-esc="event.tid"/></td>
                                <td><t t-esc="formatRssi(event.rssi_dbm)"/></td>
                            </tr>
                        </t>
                    </tbody>
                </table>
            </div>
        </div>
    `;

    setup() {
        this.orm = useService("orm");
        const params = this.props.action?.params || {};
        this.sessionId = params.session_id;
        this.timer = null;
        this.loading = false;
        this.state = useState({
            measurementCode: "",
            controllerCode: "",
            status: "",
            eventCount: 0,
            lastEventId: 0,
            summary: [],
            events: [],
            error: "",
        });
        onMounted(() => {
            this.refresh();
            this.timer = setInterval(() => this.refresh(), 1000);
        });
        onWillUnmount(() => {
            if (this.timer) {
                clearInterval(this.timer);
            }
        });
    }

    formatRssi(value) {
        return value === false || value === null || value === undefined
            ? "-"
            : `${Number(value).toFixed(1)} dBm`;
    }

    applyEventsToSummary(events) {
        const rows = new Map(
            this.state.summary.map((row) => [`${row.serial_number}-${row.antenna_no}`, {...row}])
        );
        for (const event of events) {
            const key = `${event.serial_number}-${event.antenna_no}`;
            const row = rows.get(key) || {
                serial_number: event.serial_number,
                antenna_no: event.antenna_no,
                read_count: 0,
                rssi_sample_count: 0,
                min_rssi_dbm: null,
                average_rssi_dbm: null,
                max_rssi_dbm: null,
                first_read_at: event.read_at,
                last_read_at: event.read_at,
            };
            const previousCount = Number(row.read_count || 0);
            row.read_count = previousCount + 1;
            if (event.rssi_dbm !== false && event.rssi_dbm !== null && event.rssi_dbm !== undefined) {
                const value = Number(event.rssi_dbm);
                row.min_rssi_dbm = row.min_rssi_dbm === null ? value : Math.min(Number(row.min_rssi_dbm), value);
                row.max_rssi_dbm = row.max_rssi_dbm === null ? value : Math.max(Number(row.max_rssi_dbm), value);
                const rssiCount = Number(row.rssi_sample_count || 0);
                const previousAverage = row.average_rssi_dbm === null ? value : Number(row.average_rssi_dbm);
                row.average_rssi_dbm = ((previousAverage * rssiCount) + value) / (rssiCount + 1);
                row.rssi_sample_count = rssiCount + 1;
            }
            row.first_read_at = row.first_read_at || event.read_at;
            row.last_read_at = event.read_at;
            rows.set(key, row);
        }
        this.state.summary = [...rows.values()].sort((a, b) =>
            a.serial_number.localeCompare(b.serial_number) || a.antenna_no - b.antenna_no
        );
    }

    async refresh() {
        if (!this.sessionId || this.loading) {
            return;
        }
        this.loading = true;
        try {
            const data = await this.orm.call(
                "nsp.measurement.session",
                "get_live_snapshot",
                [this.sessionId, this.state.lastEventId, 100]
            );
            if (!data?.found) {
                this.state.error = "Measurement Session was not found.";
                return;
            }
            this.state.measurementCode = data.measurement_code || "";
            this.state.controllerCode = data.controller_code || "";
            this.state.status = data.status || "";
            this.state.eventCount = data.event_count || 0;
            this.state.lastEventId = data.last_event_id || this.state.lastEventId;
            if (data.antenna_summary?.length) {
                this.state.summary = data.antenna_summary;
            } else if (data.events?.length) {
                this.applyEventsToSummary(data.events);
            }
            if (data.events?.length) {
                this.state.events = [...data.events.reverse(), ...this.state.events].slice(0, 100);
            }
            this.state.error = "";
        } catch (error) {
            this.state.error = error?.message || "Unable to load live measurement data.";
        } finally {
            this.loading = false;
        }
    }
}

registry.category("actions").add("nsp_measurement_live", NspMeasurementLive);
