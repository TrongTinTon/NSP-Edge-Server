/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

export class SaveDashboardDialog extends Component {
  static template = "t4_custom_dashboard.SaveDashboardDialog";
  static props = {
    currentDashboardId: [Number, { value: null }],
    currentDashboardName: String,
    onSave: Function,
    onCancel: Function,
  };

  setup() {
    this.state = useState({
      name: this.props.currentDashboardName || "",
      description: "",
      saveAsNew: false,
    });
  }

  validateAndSave() {
    if (!this.state.name.trim()) {
      alert("Please enter dashboard name");
      return;
    }

    const dashboardId = this.state.saveAsNew ? null : this.props.currentDashboardId;
    
    this.props.onSave(dashboardId, this.state.name, this.state.description);
  }
}