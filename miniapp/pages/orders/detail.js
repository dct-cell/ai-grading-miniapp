import { createOrderPoller } from "../../services/orders.js";
import { DownloadRefused } from "../../services/downloads.js";
import { DETAIL_ACTION_KINDS, resolveDetailActions } from "../../services/detail-actions.js";
import { decorateSummary } from "../../utils/decorate.js";
import { deliveredRounds, isActive, roundStateLabel } from "../../utils/order-states.js";
import { formatCents, formatDateTime, formatDeadline } from "../../utils/format.js";
import { ACTION_LABELS, actionsFor } from "../../services/aftersales.js";

const app = getApp();

Page({
  data: {
    orderId: "",
    order: null,
    rounds: [],
    actions: [],
    actionLabels: ACTION_LABELS,
    summary: null,
    deadlineText: "",
    downloadableRounds: [],
    loading: true,
    error: "",
    busyAction: "",
    downloading: false,
    bottomPrimaryLabel: "",
    bottomSecondaryLabel: "",
    bottomPrimaryKind: "",
    bottomSecondaryKind: "",
    bottomRoundNumber: 0,
    showHistoryDownloads: false,
    actionBusy: false,
  },

  onLoad(query) {
    this.setData({ orderId: query.id });
    this.poller = createOrderPoller({
      fetchOrder: () => app.orders.get(this.data.orderId),
      onUpdate: order => this.apply(order),
      // A transient polling failure is not worth interrupting the page for; the
      // last known state stays on screen.
      onError: () => {},
    });
  },

  onShow() {
    this.refresh().then(() => this.startPolling());
  },

  /** Both hooks stop the poller: onHide alone would leak on unload. */
  onHide() {
    this.stopPolling();
  },

  onUnload() {
    this.stopPolling();
  },

  startPolling() {
    if (this.data.order && isActive(this.data.order.state)) {
      this.poller.start();
    }
  },

  stopPolling() {
    if (this.poller) {
      this.poller.stop();
    }
  },

  async refresh() {
    try {
      await app.whenReady();
      const order = await app.orders.get(this.data.orderId);
      this.apply(order);
    } catch (error) {
      this.setData({ loading: false, error: error.detail || "加载失败。" });
    }
  },

  apply(order) {
    const downloadable = deliveredRounds(order.rounds);
    const actions = actionsFor(order.available_actions);
    const newestRound = downloadable[0] || null;
    const footer = resolveDetailActions({
      state: order.state,
      actions,
      newestRound,
      serviceTier: order.service_tier,
    });
    this.setData({
      loading: false,
      error: "",
      order: decorateSummary(order),
      rounds: (order.rounds || []).map(round => ({
        ...round,
        stateText: roundStateLabel(round.state),
        deliveredText: formatDateTime(round.delivered_at),
      })),
      // Rendered straight from the server's list: the client never decides
      // whether a refund or review is allowed.
      actions,
      downloadableRounds: downloadable,
      deadlineText: formatDeadline(order.acceptance_deadline),
      paidText: formatCents(order.paid_amount_cents),
      bottomPrimaryLabel: footer.primaryLabel,
      bottomSecondaryLabel: footer.secondaryLabel,
      bottomPrimaryKind: footer.primaryKind,
      bottomSecondaryKind: footer.secondaryKind,
      bottomRoundNumber: footer.roundNumber,
      showHistoryDownloads: footer.showHistoryDownloads,
      actionBusy: Boolean(this.data.busyAction),
    });

    if (!isActive(order.state)) {
      this.stopPolling();
    }
    if (downloadable.length > 0 && !this.data.summary) {
      this.loadSummary(downloadable[0].round_number);
    }
  },

  async loadSummary(roundNumber) {
    try {
      const summary = await app.downloads.fetchResultSummary({
        orderId: this.data.orderId,
        roundNumber,
      });
      this.setData({ summary });
    } catch (error) {
      if (error instanceof DownloadRefused) {
        // Access was revoked: re-read the order so the UI matches the server.
        this.setData({ summary: null });
        this.refresh();
      }
    }
  },

  async downloadResult(event) {
    if (this.data.downloading) {
      return;
    }
    const roundNumber = Number(event.currentTarget.dataset.round);
    this.setData({ downloading: true, error: "" });
    wx.showLoading({ title: "正在下载", mask: true });
    try {
      await app.downloads.openResultPdf({ orderId: this.data.orderId, roundNumber });
    } catch (error) {
      // Show the server's own message rather than a client-authored guess.
      this.setData({ error: error.detail || "下载失败，请稍后重试。" });
      if (error instanceof DownloadRefused && error.shouldRefresh) {
        await this.refresh();
      }
    } finally {
      wx.hideLoading();
      this.setData({ downloading: false });
    }
  },

  bottomPrimary() {
    if (this.data.bottomPrimaryKind === DETAIL_ACTION_KINDS.CREATE) {
      wx.switchTab({ url: "/pages/create/upload" });
      return;
    }
    if (this.data.bottomPrimaryKind === DETAIL_ACTION_KINDS.DOWNLOAD) {
      this.downloadResult({ currentTarget: { dataset: { round: this.data.bottomRoundNumber } } });
      return;
    }
    if (this.data.bottomPrimaryKind) {
      this.handleAction(this.data.bottomPrimaryKind);
    }
  },

  bottomSecondary() {
    if (this.data.bottomSecondaryKind === DETAIL_ACTION_KINDS.HOME) {
      wx.switchTab({ url: "/pages/home/index" });
      return;
    }
    if (this.data.bottomSecondaryKind !== DETAIL_ACTION_KINDS.ORDER_ACTIONS) {
      return;
    }
    const actions = this.data.actions || [];
    if (!actions.length || this.data.busyAction) {
      return;
    }
    wx.showActionSheet({
      itemList: actions.map(action => ACTION_LABELS[action]),
      success: result => {
        const action = actions[result.tapIndex];
        if (action) {
          this.handleAction(action);
        }
      },
    });
  },

  tapAction(event) {
    const action = event.currentTarget.dataset.action;
    this.handleAction(action);
  },

  handleAction(action) {
    if (this.data.busyAction) {
      return;
    }
    if (action === "review") {
      wx.navigateTo({ url: `/pages/aftersales/review?id=${this.data.orderId}` });
      return;
    }
    if (action === "refund") {
      wx.navigateTo({ url: `/pages/aftersales/refund?id=${this.data.orderId}` });
      return;
    }
    if (action === "accept") {
      this.confirmAccept();
    }
  },

  confirmAccept() {
    wx.showModal({
      title: "确认验收",
      content: "验收后订单完成，将不能再申请复核或退款。确定验收？",
      success: result => {
        if (result.confirm) {
          this.accept();
        }
      },
    });
  },

  async accept() {
    if (this.data.busyAction) {
      return;
    }
    // Disabled from the first tap so a double tap cannot send two requests.
    this.setData({ busyAction: "accept", actionBusy: true, error: "" });
    try {
      await app.aftersales.accept(this.data.orderId);
      wx.showToast({ title: "已验收", icon: "success" });
      await this.refresh();
    } catch (error) {
      this.setData({ error: error.detail || "验收失败，请重试。" });
      // Refresh before offering a retry: the order may have moved on.
      await this.refresh();
    } finally {
      this.setData({ busyAction: "", actionBusy: false });
    }
  },
});
