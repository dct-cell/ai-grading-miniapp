Component({
  properties: {
    file: { type: Object, value: null },
    sizeText: { type: String, value: "" },
    placeholder: { type: String, value: "选择 PDF" },
    disabled: { type: Boolean, value: false },
    progress: { type: Number, value: 0 },
    uploading: { type: Boolean, value: false },
    indeterminate: { type: Boolean, value: false },
    error: { type: String, value: "" },
  },

  methods: {
    onChoose() {
      if (this.data.disabled || this.data.uploading) {
        return;
      }
      this.triggerEvent("choose");
    },

    onClear() {
      if (this.data.disabled || this.data.uploading) {
        return;
      }
      this.triggerEvent("clear");
    },
  },
});
