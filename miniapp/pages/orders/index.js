import { FILTERS } from "../../utils/order-states.js";
import { decorateSummary } from "../../utils/decorate.js";
import { orderNavigationIntent } from "../../services/order-navigation.js";

const app = getApp();

const TABS = [
  { key: FILTERS.ALL, label: "全部" },
  { key: FILTERS.GRADING, label: "批改中" },
  { key: FILTERS.ACCEPTANCE, label: "待验收" },
];

Page({
  data: {
    tabs: TABS,
    activeTab: FILTERS.ALL,
    orders: [],
    loading: true,
    loadingMore: false,
    error: "",
    nextCursor: null,
    reachedEnd: false,
    skeletonRows: [1, 2, 3],
  },

  onShow() {
    const intent = orderNavigationIntent.consume();
    if (intent) {
      this.setData({ activeTab: intent.category }, () => {
        this.reload();
        if (intent.orderId) {
          wx.navigateTo({ url: `/pages/orders/detail?id=${intent.orderId}` });
        }
      });
      return;
    }
    this.reload();
  },

  switchTab(event) {
    const key = event.currentTarget.dataset.key;
    if (key === this.data.activeTab) {
      return;
    }
    this.setData({ activeTab: key }, () => this.reload());
  },

  async reload() {
    this.setData({ loading: true, error: "" });
    try {
      await app.whenReady();
      const page = await app.orders.list({ category: this.data.activeTab });
      this.setData({
        orders: page.items.map(decorateSummary),
        nextCursor: page.nextCursor,
        reachedEnd: page.nextCursor === null,
        loading: false,
      });
    } catch (error) {
      this.setData({ loading: false, error: error.detail || "加载失败，请下拉重试。" });
    }
  },

  /** Cursor pagination: a null cursor means the end, so stop asking. */
  async loadMore() {
    if (this.data.loadingMore || this.data.reachedEnd || !this.data.nextCursor) {
      return;
    }
    this.setData({ loadingMore: true });
    try {
      const page = await app.orders.list({
        category: this.data.activeTab,
        cursor: this.data.nextCursor,
      });
      this.setData({
        orders: this.data.orders.concat(page.items.map(decorateSummary)),
        nextCursor: page.nextCursor,
        reachedEnd: page.nextCursor === null,
        loadingMore: false,
      });
    } catch (error) {
      this.setData({ loadingMore: false, error: error.detail || "加载失败。" });
    }
  },

  onReachBottom() {
    this.loadMore();
  },

  onPullDownRefresh() {
    this.reload().then(() => wx.stopPullDownRefresh());
  },

  openOrder(event) {
    // The card reports its own id through the event detail.
    const orderId = event.detail.orderId;
    wx.navigateTo({ url: `/pages/orders/detail?id=${orderId}` });
  },

  goCreate() {
    wx.switchTab({ url: "/pages/create/upload" });
  },
});
