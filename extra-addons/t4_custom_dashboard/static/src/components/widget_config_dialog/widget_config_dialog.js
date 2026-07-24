/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";

export class WidgetConfigDialog extends Component {
  static template = "t4_custom_dashboard.WidgetConfigDialog";
  static props = {
    widget: { type: Object, optional: true },
    searchPanelConfig: { optional: true },
    onSave: { type: Function },
    onCancel: { type: Function },
  };

  setup() {
    const w = this.props.widget || {};
    this.state = useState({
      // Basic config
      widgetType: w.type || "stat",
      title: w.data?.title || "",

      // NSP dashboard runtime uses Python data sources only.
      dataSourceType: "python",

      // Python function config
      pythonModel: w.dataSource?.pythonModel || "",
      pythonMethod: w.dataSource?.pythonMethod || "",
      pythonParams: JSON.stringify(w.dataSource?.pythonParams || {}),

      // Display config (for stat)
      icon: w.data?.icon || "fa-chart-bar",
      bgColor: w.data?.bgColor || "#7c3aed",
      textColor: w.data?.textColor || "#ffffff",

      // Chart config
      chartType: w.data?.chartType || "bar",
      chartBgColor: w.data?.chartColors?.background || "#7c3aed",
      chartBorderColor: w.data?.chartColors?.border || "#5b21b6",

      // 🆕 Combo chart — màu dataset Right + label 2 trục
      chartBgColorRight: w.data?.chartColorsRight?.background || "#0ea5e9",
      chartBorderColorRight: w.data?.chartColorsRight?.border || "#0369a1",
      labelLeft: w.data?.label_left || "Trục Trái",
      labelRight: w.data?.label_right || "Trục Phải",

      // Axis labels configuration
      xAxisLabel: w.data?.xAxisLabel || "Danh mục",
      yAxisLabel: w.data?.yAxisLabel || "Số lượng",

      // 🆕 TOP limit - checkbox để enable/disable
      enableTopLimit: w.data?.enableTopLimit || false,
      topLimit: w.data?.topLimit || 10,

      // 🆕 stat_action + chart drill-down: action config
      actionModel: w.action?.model || "",
      actionName: w.action?.name || "",
      actionDomainMethod: w.action?.domain_method || "",
      actionDomainParams: JSON.stringify(w.action?.domain_params || {}),
      actionDomainModel: w.action?.domain_model || "",
      actionViewMode: w.action?.view_mode || "list,form",
      actionContext: JSON.stringify(w.action?.context || {}),
      // 🆕 action_xml_id: tham chiếu action có sẵn của t4_sti (giữ context HID/RFID + view t4_)
      actionXmlId: w.action?.action_xml_id || "",
      // 🆕 Chart drill-down toggle (action là optional cho chart)
      chartEnableDrill: !!(w.type === "chart" && w.action && w.action.model),

      // 🆕 kanban_embed: embed view config
      embedModel: w.embed?.model || "",
      embedViewXmlId: w.embed?.view_xml_id || "",
      embedViewType: w.embed?.view_type || "kanban",
      embedContext: JSON.stringify(w.embed?.context || {}),
      embedDomain: JSON.stringify(w.embed?.domain || []),

      // Filter overrides: { [filterId]: fieldPathString }
      filterOverrides: { ...(w.dataSource?.filterOverrides || {}) },

      // UI state
      loading: false,
      showFilterOverrides: false,
    });

    // Predefined color schemes for quick selection
    this.colorSchemes = [
      { name: "Purple", bg: "#7c3aed", text: "#ffffff", border: "#5b21b6" },
      { name: "Green", bg: "#16a34a", text: "#ffffff", border: "#15803d" },
      { name: "Blue", bg: "#0284c7", text: "#ffffff", border: "#0369a1" },
      { name: "Orange", bg: "#ea580c", text: "#ffffff", border: "#c2410c" },
      { name: "Red", bg: "#dc2626", text: "#ffffff", border: "#b91c1c" },
      { name: "Pink", bg: "#db2777", text: "#ffffff", border: "#be185d" },
      { name: "Indigo", bg: "#4f46e5", text: "#ffffff", border: "#4338ca" },
      { name: "Teal", bg: "#0d9488", text: "#ffffff", border: "#0f766e" },
      { name: "Yellow", bg: "#ca8a04", text: "#ffffff", border: "#a16207" },
      { name: "Gray", bg: "#64748b", text: "#ffffff", border: "#475569" },
    ];

    // Chart preview data
    this.previewChartData = {
      labels: ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
      values: [12, 19, 8, 15, 22, 16],
    };
  }

  applyColorScheme(scheme) {
    if (this.isStatLikeWidget) {
      this.state.bgColor = scheme.bg;
      this.state.textColor = scheme.text;
    } else if (this.isChartWidget) {
      this.state.chartBgColor = scheme.bg;
      this.state.chartBorderColor = scheme.border;
    }
  }

  get isStatWidget() {
    return this.state.widgetType === "stat";
  }

  get isStatActionWidget() {
    return this.state.widgetType === "stat_action";
  }

  get isStatLikeWidget() {
    // stat + stat_action share visual config (icon, color)
    return this.isStatWidget || this.isStatActionWidget;
  }

  get isChartWidget() {
    return this.state.widgetType === "chart";
  }

  get isKanbanEmbedWidget() {
    return this.state.widgetType === "kanban_embed";
  }

  get needsDataSource() {
    // kanban_embed không dùng dataSource (data lấy từ view trực tiếp)
    return !this.isKanbanEmbedWidget;
  }

  get actionConfigVisible() {
    // Hiện UI cấu hình action drill-down khi:
    // - widget là stat_action (bắt buộc có action)
    // - widget là chart và user bật toggle drill-down
    return (
      this.isStatActionWidget ||
      (this.isChartWidget && this.state.chartEnableDrill)
    );
  }

  get actionConfigRequired() {
    // stat_action: bắt buộc; chart: optional (chỉ check khi toggle bật)
    return this.actionConfigVisible;
  }

  get isPythonSource() {
    return this.state.dataSourceType === "python";
  }

  get chartTypeIcon() {
    const icons = {
      bar: "fa-bar-chart",
      line: "fa-line-chart",
      pie: "fa-pie-chart",
      doughnut: "fa-pie-chart",
      combo: "fa-bar-chart",
    };
    return icons[this.state.chartType] || "fa-bar-chart";
  }

  get chartTypeLabel() {
    const labels = {
      bar: "Bar Chart",
      line: "Line Chart",
      pie: "Pie Chart",
      doughnut: "Doughnut Chart",
      combo: "Combo (Dual-Axis)",
    };
    return labels[this.state.chartType] || "Bar Chart";
  }

  get isComboChart() {
    return this.state.chartType === "combo";
  }

  get statGradientStyle() {
    const bgColor = this.state.bgColor;
    const darkerColor = this.darkenColor(bgColor, 20);
    return `background: linear-gradient(135deg, ${bgColor} 0%, ${darkerColor} 100%);`;
  }

  darkenColor(hex, percent) {
    hex = hex.replace("#", "");
    const num = parseInt(hex, 16);
    const amt = Math.round(2.55 * percent);
    const R = Math.max(0, Math.min(255, (num >> 16) - amt));
    const G = Math.max(0, Math.min(255, ((num >> 8) & 0x00ff) - amt));
    const B = Math.max(0, Math.min(255, (num & 0x0000ff) - amt));
    return (
      "#" + (0x1000000 + R * 0x10000 + G * 0x100 + B).toString(16).slice(1)
    );
  }

  validateAndSave() {
    // Validation
    if (!this.state.title) {
      alert("Please enter a title");
      return;
    }

    // kanban_embed: validate embed config riêng (không dùng dataSource)
    if (this.isKanbanEmbedWidget) {
      if (!this.state.embedModel) {
        alert("Vui lòng nhập Model cho kanban embed");
        return;
      }
      try {
        if (this.state.embedContext) JSON.parse(this.state.embedContext);
        if (this.state.embedDomain) JSON.parse(this.state.embedDomain);
      } catch (e) {
        alert("Context/Domain JSON không hợp lệ");
        return;
      }
    } else {
      // Validate data source cho stat / stat_action / chart
      if (this.isPythonSource) {
        if (!this.state.pythonModel || !this.state.pythonMethod) {
          alert("Please enter Python model and method");
          return;
        }
        try {
          if (this.state.pythonParams) JSON.parse(this.state.pythonParams);
        } catch (error) {
          alert("Invalid Python parameters format. Use valid JSON object.");
          return;
        }
      }
    }

    // stat_action + chart-with-drill: validate action config
    if (this.actionConfigRequired) {
      // Cho phép 1 trong 2:
      // - action_xml_id (tham chiếu action có sẵn của t4_sti) + domain_method
      // - action_model + domain_method (build action từ raw fields)
      const hasXmlId = !!this.state.actionXmlId;
      const hasModel = !!this.state.actionModel;
      if (!hasXmlId && !hasModel) {
        alert("Vui lòng nhập Action XML ID hoặc Action Model cho drill-down");
        return;
      }
      if (!this.state.actionDomainMethod) {
        alert("Vui lòng nhập Domain Method cho drill-down");
        return;
      }
      try {
        if (this.state.actionDomainParams)
          JSON.parse(this.state.actionDomainParams);
      } catch (e) {
        alert("Action domain params JSON không hợp lệ");
        return;
      }
      try {
        if (this.state.actionContext) JSON.parse(this.state.actionContext);
      } catch (e) {
        alert("Action context JSON không hợp lệ");
        return;
      }
    }

    // Default w/h theo widget type
    // stat/stat_action h=2 (~180px) đủ chỗ cho icon + title + value;
    // SCSS dùng container queries scale content theo width nên card vẫn
    // đẹp ở w nhỏ.
    const sizeDefaults = {
      stat: { w: 3, h: 2 },
      stat_action: { w: 3, h: 2 },
      chart: { w: 6, h: 4 },
      kanban_embed: { w: 12, h: 5 },
    };
    const size = sizeDefaults[this.state.widgetType] || { w: 6, h: 4 };

    // Build widget config
    const widgetConfig = {
      id: this.props.widget?.id || `widget_${Date.now()}`,
      type: this.state.widgetType,
      x: this.props.widget?.x || 0,
      y: this.props.widget?.y || 0,
      w: this.props.widget?.w || size.w,
      h: this.props.widget?.h || size.h,
      data: {
        title: this.state.title,
      },
    };

    // Add data source (skip kanban_embed)
    if (this.needsDataSource) {
      widgetConfig.dataSource = { type: "python" };
      if (this.isPythonSource) {
        widgetConfig.dataSource.pythonModel = this.state.pythonModel;
        widgetConfig.dataSource.pythonMethod = this.state.pythonMethod;
        try {
          widgetConfig.dataSource.pythonParams = JSON.parse(
            this.state.pythonParams || "{}"
          );
        } catch (error) {
          widgetConfig.dataSource.pythonParams = {};
        }
      }
    }

    // stat + stat_action: visual config
    if (this.isStatLikeWidget) {
      widgetConfig.data.icon = this.state.icon;
      widgetConfig.data.bgColor = this.state.bgColor;
      widgetConfig.data.textColor = this.state.textColor;
    }

    // stat_action + chart-with-drill: build action block
    if (this.actionConfigVisible) {
      widgetConfig.action = {};
      // Ưu tiên action_xml_id (Path A — giữ context HID/RFID + view t4_)
      if (this.state.actionXmlId) {
        widgetConfig.action.action_xml_id = this.state.actionXmlId;
      } else {
        // Path B — build action từ raw fields
        widgetConfig.action.model = this.state.actionModel;
        widgetConfig.action.view_mode = this.state.actionViewMode;
      }
      widgetConfig.action.name = this.state.actionName || this.state.title;
      widgetConfig.action.domain_method = this.state.actionDomainMethod;
      if (this.state.actionDomainModel) {
        widgetConfig.action.domain_model = this.state.actionDomainModel;
      }
      try {
        widgetConfig.action.domain_params = JSON.parse(
          this.state.actionDomainParams || "{}"
        );
      } catch (e) {
        widgetConfig.action.domain_params = {};
      }
      try {
        const ctx = JSON.parse(this.state.actionContext || "{}");
        if (Object.keys(ctx).length > 0) widgetConfig.action.context = ctx;
      } catch (e) {
        // skip
      }
    }

    // kanban_embed: embed config
    if (this.isKanbanEmbedWidget) {
      widgetConfig.embed = {
        model: this.state.embedModel,
        view_type: this.state.embedViewType || "kanban",
      };
      if (this.state.embedViewXmlId) {
        widgetConfig.embed.view_xml_id = this.state.embedViewXmlId;
      }
      try {
        widgetConfig.embed.context = JSON.parse(
          this.state.embedContext || "{}"
        );
      } catch (e) {
        widgetConfig.embed.context = {};
      }
      try {
        widgetConfig.embed.domain = JSON.parse(
          this.state.embedDomain || "[]"
        );
      } catch (e) {
        widgetConfig.embed.domain = [];
      }
    }

    if (this.isChartWidget) {
      widgetConfig.data.chartType = this.state.chartType;
      widgetConfig.data.chartColors = {
        background: this.state.chartBgColor,
        border: this.state.chartBorderColor,
      };
      widgetConfig.data.xAxisLabel = this.state.xAxisLabel;
      widgetConfig.data.yAxisLabel = this.state.yAxisLabel;

      // 🆕 Combo-specific config
      if (this.state.chartType === "combo") {
        widgetConfig.data.chartColorsRight = {
          background: this.state.chartBgColorRight,
          border: this.state.chartBorderColorRight,
        };
        widgetConfig.data.label_left = this.state.labelLeft;
        widgetConfig.data.label_right = this.state.labelRight;
      }

      // 🆕 Lưu TOP limit config
      widgetConfig.data.enableTopLimit = this.state.enableTopLimit;
      widgetConfig.data.topLimit = this.state.enableTopLimit && this.state.topLimit > 0
        ? parseInt(this.state.topLimit)
        : null;
    }

    // Persist filter overrides — drop keys with empty values (use filter default)
    const overrides = {};
    for (const [filterId, fieldPath] of Object.entries(this.state.filterOverrides)) {
      if (fieldPath && fieldPath.trim()) {
        overrides[filterId] = fieldPath.trim();
      }
    }
    if (Object.keys(overrides).length > 0 && widgetConfig.dataSource) {
      widgetConfig.dataSource.filterOverrides = overrides;
    }

    this.props.onSave(widgetConfig);
  }

  // -------------------------------------------------------------------------
  // Filter overrides helpers
  // -------------------------------------------------------------------------

  get searchPanelFilters() {
    const cfg = this.props.searchPanelConfig;
    if (!Array.isArray(cfg) || cfg.length === 0) return [];
    return cfg;
  }

  setFilterOverride(filterId, value) {
    this.state.filterOverrides[filterId] = value;
  }
}