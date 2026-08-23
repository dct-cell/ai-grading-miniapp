import { formatCents, formatDateTime, formatEta, standardLabel, GRADING_STANDARDS } from "../../utils/format.js";
import { displayLabel, filterForState, FILTERS, stateTone } from "../../utils/order-states.js";

const app = getApp();

Page({
  data: {
    loading: true,
    error: "",
    isNewUser: true,
    standards: GRADING_STANDARDS,
    unitPriceText: "",
    activeOrder: null,
    recentDelivered: null,
    publicId: "",
    accountText: "我",
  },

  onShow() {
    this.load();
  },

  async load() {
    this.setData({ loading: true, error: "" });
    try {
      const user = await app.whenReady();
      // The first page of the user's own orders is enough to decide between the
      // onboarding view and the returning view.
      const page = await app.api.get("/api/v1/orders?category=all&limit=5");
      const items = page.items || [];
      const active = items.find(item => filterForState(item.state) === FILTERS.GRADING) || null;
      const delivered =
        items.find(item => filterForState(item.state) === FILTERS.ACCEPTANCE) || null;

      this.setData({
        loading: false,
        publicId: (user && user.public_id) || "",
        accountText: user && user.public_id ? String(user.public_id).slice(-1).toUpperCase() : "我",
        isNewUser: items.length === 0,
        activeOrder: active ? this.decorate(active) : null,
        recentDelivered: delivered ? this.decorate(delivered) : null,
      });
    } catch (error) {
      this.setData({ loading: false, error: error.detail || "加载失败，请下拉重试。" });
    }
  },

  decorate(order) {
    return {
      ...order,
      stateText: displayLabel(order.state, order.rounds),
      tone: stateTone(order.state),
      standardText: standardLabel(order.grading_standard),
      amountText: formatCents(order.paid_amount_cents),
      etaText: formatEta(order.eta),
      createdText: formatDateTime(order.created_at),
    };
  },

  onPullDownRefresh() {
    this.load().then(() => wx.stopPullDownRefresh());
  },

  goCreate() {
    wx.switchTab({ url: "/pages/create/upload" });
  },

  goOrders() {
    wx.switchTab({ url: "/pages/orders/index" });
  },

  openOrder(event) {
    const { id } = event.currentTarget.dataset;
    wx.navigateTo({ url: `/pages/orders/detail?id=${id}` });
  },

  goAccount() {
    wx.navigateTo({ url: "/pages/account/index" });
  },
});
