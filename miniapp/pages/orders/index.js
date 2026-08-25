import { FILTERS, filterForState, isGradingState } from "../../utils/order-states.js";
import { decorateSummary } from "../../utils/decorate.js";
import { orderNavigationIntent } from "../../services/order-navigation.js";
import { createOrderProgressPoller } from "../../services/orders.js";

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

  onLoad() {
    this.progressPoller = createOrderProgressPoller({
      fetchProgress: () =>
        app.orders.progress(this.data.orders.map(order => order.id)),
      onUpdate: progress => this.applyProgress(progress),
      // Keep the last known stage on a transient network failure.
      onError: () => {},
    });
  },

  onShow() {
    this.stopProgressPolling();
    const intent = orderNavigationIntent.consume();
    if (intent) {
      this.setData({ activeTab: intent.category }, () => {
        this.reload().then(() => this.startProgressPolling());
        if (intent.orderId) {
          wx.navigateTo({ url: `/pages/orders/detail?id=${intent.orderId}` });
        }
      });
      return;
    }
    this.reload().then(() => this.startProgressPolling());
  },

  onHide() {
    this.stopProgressPolling();
  },

  onUnload() {
    this.stopProgressPolling();
  },

  switchTab(event) {
    const key = event.currentTarget.dataset.key;
    if (key === this.data.activeTab) {
      return;
    }
    this.stopProgressPolling();
    this.setData({ activeTab: key }, () => {
      this.reload().then(() => this.startProgressPolling());
    });
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

  startProgressPolling() {
    if (
      this.progressPoller &&
      this.data.orders.some(order => isGradingState(order.state))
    ) {
      this.progressPoller.start();
    }
  },

  stopProgressPolling() {
    if (this.progressPoller) {
      this.progressPoller.stop();
    }
  },

  applyProgress(progress) {
    const updates = new Map(
      ((progress && progress.items) || []).map(item => [item.id, item]),
    );
    const activeTab = this.data.activeTab;
    const orders = this.data.orders
      .map(order => {
        const update = updates.get(order.id);
        return decorateSummary(update ? { ...order, ...update } : order);
      })
      .filter(order =>
        activeTab === FILTERS.ALL || filterForState(order.state) === activeTab,
      );
    this.setData({ orders });
    if (!orders.some(order => isGradingState(order.state))) {
      this.stopProgressPolling();
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
