import { REFUND_REASONS, describeRefundOutcome } from "../../services/aftersales.js";
import { formatCents } from "../../utils/format.js";

const app = getApp();

/**
 * Full refund request.
 *
 * The amount is displayed, never edited: the server always refunds the whole
 * paid amount and ignores any client-supplied figure. This page also never
 * decides whether the refund qualifies for automatic processing — it reports
 * whichever outcome the server returns.
 */
Page({
  data: {
    orderId: "",
    order: null,
    reasons: REFUND_REASONS,
    reason: "",
    amountText: "",
    submitting: false,
    error: "",
    allowed: false,
    outcome: null,
  },

  async onLoad(query) {
    this.setData({ orderId: query.id });
    await this.loadOrder();
  },

  async loadOrder() {
    try {
      await app.whenReady();
      const order = await app.orders.get(this.data.orderId);
      const allowed = (order.available_actions || []).includes("refund");
      this.setData({
        order,
        allowed,
        // Straight from the server's record of what was paid.
        amountText: formatCents(order.paid_amount_cents),
        error: allowed ? "" : "当前订单不支持退款。",
      });
    } catch (error) {
      this.setData({ error: error.detail || "加载失败。" });
    }
  },

  chooseReason(event) {
    if (this.data.submitting) {
      return;
    }
    this.setData({ reason: event.currentTarget.dataset.value });
  },

  confirm() {
    if (!this.data.allowed || this.data.submitting) {
      return;
    }
    if (!this.data.reason) {
      this.setData({ error: "请选择退款原因。" });
      return;
    }
    wx.showModal({
      title: "确认申请退款",
      content: `将申请全额退款 ${this.data.amountText}。退款成功后将无法再下载批改结果。`,
      success: result => {
        if (result.confirm) {
          this.submit();
        }
      },
    });
  },

  async submit() {
    if (this.data.submitting) {
      return;
    }
    // Disabled from the first tap. The server also rejects a second refund
    // (one live refund per payment), but a double request must not be sent.
    this.setData({ submitting: true, error: "" });
    try {
      // 202 Accepted. `state` is either `refunded` (already settled) or
      // `refund_pending` (awaiting an Admin decision).
      const outcome = await app.aftersales.refund(this.data.orderId, {
        reason: this.data.reason,
      });
      this.setData({ submitting: false, outcome: describeRefundOutcome(outcome) });
    } catch (error) {
      this.setData({ submitting: false, error: error.detail || "提交失败，请重试。" });
      // Refresh before a retry: the order may have changed underneath.
      await this.loadOrder();
    }
  },

  done() {
    wx.redirectTo({ url: `/pages/orders/detail?id=${this.data.orderId}` });
  },

  cancel() {
    wx.navigateBack();
  },
});
