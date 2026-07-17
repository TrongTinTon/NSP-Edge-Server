/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";

export class NspParkingDisplay extends Component {
    static template = xml`
        <div class="nsp-parking-display">
            <div class="nsp-parking-display__header">
                <div>
                    <div class="nsp-parking-display__title">PARKING DISPLAY</div>
                    <div class="nsp-parking-display__subtitle">Realtime Parking Notifications from Gatekeeper Transactions</div>
                </div>
                <div class="nsp-parking-display__clock"><t t-esc="state.clock"/></div>
            </div>

            <div class="nsp-parking-display__selector">
                <label>Gate</label>
                <select t-on-change="onGateChange">
                    <option value="">Select Gate before display</option>
                    <t t-foreach="state.gates" t-as="gate" t-key="gate.id">
                        <option t-att-value="gate.id">
                            <t t-esc="gate.display_name || gate.name || gate.code"/>
                        </option>
                    </t>
                </select>
            </div>

            <t t-if="state.selectedGateId">
                <div class="nsp-parking-display__ticker-wrap">
                    <div class="nsp-parking-display__ticker" t-att-class="state.items.length ? '' : 'is-empty'">
                        <t t-if="state.items.length">
                            <t t-foreach="animatedItems" t-as="item" t-key="item.key">
                                <div class="nsp-parking-display__plate-card" t-att-class="item.status === 'denied' ? 'is-denied' : 'is-allowed'">
                                    <div class="nsp-parking-display__plate"><t t-esc="item.vehicle"/></div>
                                    <div class="nsp-parking-display__meta">
                                        <span><t t-esc="item.gate"/></span>
                                        <span>•</span>
                                        <span><t t-esc="item.direction_label"/></span>
                                        <span>•</span>
                                        <span><t t-esc="item.event_time"/></span>
                                    </div>
                                    <div class="nsp-parking-display__status" t-if="item.status === 'denied'">
                                        DENIED <span t-if="item.message">- <t t-esc="item.message"/></span>
                                    </div>
                                </div>
                            </t>
                        </t>
                        <t t-else="">
                            <div class="nsp-parking-display__empty">WAITING FOR VEHICLE ENTRY / EXIT</div>
                        </t>
                    </div>
                </div>

                <div class="nsp-parking-display__list-title">Latest Events</div>
                <div class="nsp-parking-display__list">
                    <t t-if="state.items.length">
                        <t t-foreach="state.items" t-as="item" t-key="item.id">
                            <div class="nsp-parking-display__row" t-att-class="item.status === 'denied' ? 'is-denied' : 'is-allowed'">
                                <div class="nsp-parking-display__row-plate"><t t-esc="item.vehicle"/></div>
                                <div class="nsp-parking-display__row-detail">
                                    <span><t t-esc="item.gate"/></span>
                                    <span><t t-esc="item.direction_label"/></span>
                                    <span><t t-esc="item.status_label"/></span>
                                    <span><t t-esc="item.event_time"/></span>
                                </div>
                            </div>
                        </t>
                    </t>
                    <t t-else="">
                        <div class="nsp-parking-display__row is-empty">No realtime data yet.</div>
                    </t>
                </div>
            </t>
            <t t-else="">
                <div class="nsp-parking-display__choose-gate">Please select a Gate to start realtime display.</div>
            </t>
        </div>
    `;

    setup() {
        this.state = useState({
            items: [],
            gates: [],
            selectedGateId: "",
            clock: "",
        });
        this.pollTimer = null;
        this.clockTimer = null;
        this.seenIds = new Set();

        onMounted(() => {
            this.tickClock();
            this.fetchGates();
            this.pollTimer = setInterval(() => this.fetchEvents(), 1000);
            this.clockTimer = setInterval(() => this.tickClock(), 1000);
        });

        onWillUnmount(() => {
            if (this.pollTimer) {
                clearInterval(this.pollTimer);
            }
            if (this.clockTimer) {
                clearInterval(this.clockTimer);
            }
        });
    }

    get animatedItems() {
        const items = this.state.items.length ? this.state.items : [];
        return [...items, ...items].map((item, index) => ({
            ...item,
            key: `${item.id}-${index}`,
        }));
    }

    onGateChange(ev) {
        this.state.selectedGateId = ev.target.value || "";
        this.state.items = [];
        this.seenIds = new Set();
        if (this.state.selectedGateId) {
            this.fetchEvents();
        }
    }

    tickClock() {
        const now = new Date();
        this.state.clock = now.toLocaleString("en-GB", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
        });
    }

    async fetchGates() {
        try {
            const response = await fetch("/api/nsp_gatekeeper/v1/parking-display/gates", {
                method: "GET",
                credentials: "same-origin",
                headers: { "Accept": "application/json" },
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            this.state.gates = payload.gates || [];
        } catch (error) {
            console.warn("NSP Parking Display: unable to fetch gates", error);
        }
    }

    async fetchEvents() {
        if (!this.state.selectedGateId) {
            return;
        }
        try {
            const url = `/api/nsp_notification/v1/parking-monitor/events?limit=30&gate_id=${encodeURIComponent(this.state.selectedGateId)}`;
            const response = await fetch(url, {
                method: "GET",
                credentials: "same-origin",
                headers: { "Accept": "application/json" },
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            const events = payload.events || [];
            this.state.items = events.slice(-80);
        } catch (error) {
            console.warn("NSP Parking Display: unable to fetch events", error);
        }
    }
}

registry.category("actions").add("nsp_parking_display", NspParkingDisplay);
