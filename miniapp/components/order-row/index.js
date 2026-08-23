Component({
  properties: {
    order: { type: Object, value: null },
  },

  methods: {
    open() {
      if (!this.data.order) {
        return;
      }
      this.triggerEvent("open", { orderId: this.data.order.id });
    },
  },
});
