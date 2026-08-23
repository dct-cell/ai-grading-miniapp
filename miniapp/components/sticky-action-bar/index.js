Component({
  properties: {
    primaryLabel: { type: String, value: "继续" },
    secondaryLabel: { type: String, value: "" },
    disabled: { type: Boolean, value: false },
    busy: { type: Boolean, value: false },
    danger: { type: Boolean, value: false },
  },

  methods: {
    primary() {
      if (this.data.disabled || this.data.busy) {
        return;
      }
      this.triggerEvent("primary");
    },
    secondary() {
      if (this.data.busy) {
        return;
      }
      this.triggerEvent("secondary");
    },
  },
});
