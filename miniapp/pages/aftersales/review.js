import { MAX_REVIEW_CHARS, reviewTextError } from "../../services/aftersales.js";
import { formatDeadline, standardLabel } from "../../utils/format.js";

const app = getApp();

/**
 * V1 review: buy the single second grading round.
 *
 * There is deliberately **no file picker** here. A review re-grades the exact
 * same immutable PDF that was paid for, so the page shows the original file's
 * identity instead of letting a different document be substituted.
 */
Page({
  data: {
    orderId: "",
    order: null,
    text: "",
    textLength: 0,
    maxChars: MAX_REVIEW_CHARS,
    textError: "",
    deadlineText: "",
    standardText: "",
    submitting: false,
    error: "",
    canSubmit: false,
    allowed: false,
    focused: false,
  },

  async onLoad(query) {
    this.setData({ orderId: query.id });
    await this.loadOrder();
  },

  async loadOrder() {
    try {
      await app.whenReady();
      const order = await app.orders.get(this.data.orderId);
      const allowed = (order.available_actions || []).includes("review");
      this.setData({
        order,
        // Authority stays with the server: no review button, no submission.
        allowed,
        standardText: standardLabel(order.grading_standard),
        deadlineText: formatDeadline(order.acceptance_deadline),
        error: allowed ? "" : "当前订单不支持复核。",
      });
      this.validate();
    } catch (error) {
      this.setData({ error: error.detail || "加载失败。" });
    }
  },

  onTextInput(event) {
    const text = event.detail.value;
    this.setData({ text, textLength: text.length });
    this.validate();
  },

  onFocus() {
    this.setData({ focused: true });
  },

  onBlur() {
    this.setData({ focused: false });
  },

  validate() {
    const message = reviewTextError(this.data.text);
    this.setData({
      // Only shown once the user has typed something, to avoid nagging.
      textError: this.data.text.length > 0 ? message : "",
      canSubmit: this.data.allowed && message === "" && !this.data.submitting,
    });
  },

  async submit() {
    if (!this.data.canSubmit || this.data.submitting) {
      return;
    }
    const message = reviewTextError(this.data.text);
    if (message) {
      this.setData({ textError: message });
      return;
    }

    // Disabled from the first tap: a second review is rejected by the server
    // (Appeal.order_id is unique), and a duplicate request would just confuse.
    this.setData({ submitting: true, canSubmit: false, error: "" });
    try {
      // 202 Accepted: a Worker grades the new round later.
      await app.aftersales.review(this.data.orderId, { text: this.data.text });
      wx.showToast({ title: "复核已提交", icon: "success" });
      // Replace this page so the back gesture cannot resubmit.
      wx.redirectTo({ url: `/pages/orders/detail?id=${this.data.orderId}` });
    } catch (error) {
      this.setData({
        submitting: false,
        error: error.detail || "提交失败，请重试。",
      });
      // Re-read the order before offering a retry: it may have moved on.
      await this.loadOrder();
    }
  },

  cancel() {
    wx.navigateBack();
  },
});
