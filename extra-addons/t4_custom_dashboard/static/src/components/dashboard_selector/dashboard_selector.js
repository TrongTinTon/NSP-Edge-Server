/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

export class DashboardSelector extends Component {
  static template = "t4_custom_dashboard.DashboardSelector";
  static props = {
    dashboards: Array,
    currentDashboardId: [Number, { value: null }],
    currentDashboardName: String,
    onSwitch: Function,
    onDelete: Function,
    onSetDefault: Function,
    onDuplicate: Function,
    onClose: Function,
  };

  setup() {
    this.state = useState({
      searchText: "",
    });
  }

  get filteredDashboards() {
    if (!this.state.searchText) {
      return this.props.dashboards;
    }
    
    const search = this.state.searchText.toLowerCase();
    return this.props.dashboards.filter(d => 
      d.name.toLowerCase().includes(search) ||
      (d.description && d.description.toLowerCase().includes(search))
    );
  }

  switchDashboard(dashboardId) {
    this.props.onSwitch(dashboardId);
    this.props.onClose();
  }

  deleteDashboard(event, dashboardId) {
    event.stopPropagation();
    this.props.onDelete(dashboardId);
  }

  setDefaultDashboard(event, dashboardId) {
    event.stopPropagation();
    this.props.onSetDefault(dashboardId);
  }

  duplicateDashboard(event, dashboardId) {
    event.stopPropagation();
    this.props.onDuplicate(dashboardId);
  }
}