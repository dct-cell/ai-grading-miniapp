Component({
  properties: {
    options: { type: Array, value: [] },
    value: { type: String, value: "" },
    disabled: { type: Boolean, value: false },
  },

  methods: {
    choose(event) {
      if (this.data.disabled) {
        return;
      }
      this.triggerEvent("change", { value: event.currentTarget.dataset.value });
    },
  },
});
