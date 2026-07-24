/** @odoo-module **/

import { Component, useState, useRef } from "@odoo/owl";

export class ImportExportDialog extends Component {
  static template = "t4_custom_dashboard.ImportExportDialog";
  static props = {
    mode: String, // 'export' or 'import'
    onExport: Function,
    onImport: Function,
    onClose: Function,
  };

  setup() {
    this.fileInputRef = useRef("fileInput");
    
    this.state = useState({
      importName: "",
      fileContent: null,
      fileName: "",
    });
  }

  handleFileSelect(event) {
    const file = event.target.files[0];
    if (!file) return;

    this.state.fileName = file.name;

    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        this.state.fileContent = e.target.result;
        
        // Try to parse to validate
        const config = JSON.parse(this.state.fileContent);
        
        // Auto-fill name from config
        if (config.name && !this.state.importName) {
          this.state.importName = config.name;
        }
      } catch (error) {
        alert("Invalid JSON file");
        this.state.fileContent = null;
        this.state.fileName = "";
      }
    };
    reader.readAsText(file);
  }

  async handleImport() {
    if (!this.state.fileContent) {
      alert("Please select a file");
      return;
    }

    const success = await this.props.onImport(
      this.state.fileContent,
      this.state.importName || null
    );

    if (success) {
      this.props.onClose();
    }
  }

  handleExport() {
    this.props.onExport();
    this.props.onClose();
  }
}