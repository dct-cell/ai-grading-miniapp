Component({
  properties: {
    order: { type: Object, value: null },
  },

  methods: {
    onOpen() {
      this.triggerEvent("open", { orderId: this.data.order.id });
    },
  },
});
