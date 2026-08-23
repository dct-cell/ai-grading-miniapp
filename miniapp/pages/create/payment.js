import { createDraft } from "../../services/create-draft.js";
import { PaymentCancelled, PaymentUnconfirmed } from "../../services/payments.js";
import { orderNavigationIntent } from "../../services/order-navigation.js";
import { formatCents, serviceTierDelivery, serviceTierLabel, standardLabel } from "../../utils/format.js";
import { FILTERS, filterForState } from "../../utils/order-states.js";
import { usesSimulatedPayment } from "../../config.js";

const app = getApp();

/**
 * Step 3 of 3: confirm and pay.
 *
 * Every displayed amount comes from the quote the server issued. The page never
 * multiplies pages by a unit price, and it never treats the payment sheet's
 * success callback as proof of payment — `payAndConfirm` waits for the server.
 */
Page({
  data: {
    quote: null,
    sourceName: "",
    referenceName: "",
    standardText: "",
    serviceTierText: "",
    deliveryText: "",
    unitPriceText: "",
    totalText: "",
    expiresInText: "",
    paying: false,
    error: "",
    simulated: false,
  },

  onLoad() {
    const state = createDraft.getState();
    if (!state.quote) {
      wx.redirectTo({ url: "/pages/create/upload" });
      return;
    }
    this.setData({
      quote: state.quote,
      sourceName: state.sourcePdf ? state.sourcePdf.name : "",
      referenceName: state.referencePdf ? state.referencePdf.name : "",
      standardText: standardLabel(state.quote.grading_standard || state.standard),
      serviceTierText: serviceTierLabel(state.quote.service_tier || state.serviceTier),
      deliveryText: serviceTierDelivery(state.quote.service_tier || state.serviceTier),
      unitPriceText: formatCents(state.quote.cents_per_page),
      totalText: formatCents(state.quote.amount_cents),
      expiresInText: this.describeExpiry(state.quote.expires_in_seconds),
      simulated: usesSimulatedPayment(app.globalData.profile),
    });
  },

  describeExpiry(seconds) {
    if (typeof seconds !== "number" || seconds <= 0) {
      return "报价已过期，请重新上传。";
    }
    const hours = Math.floor(seconds / 3600);
    if (hours >= 1) {
      return `报价 ${hours} 小时内有效`;
    }
    return `报价 ${Math.max(1, Math.floor(seconds / 60))} 分钟内有效`;
  },

  async pay() {
    if (this.data.paying) {
      return;
    }
    this.setData({ paying: true, error: "" });
    wx.showLoading({ title: "支付确认中", mask: true });

    let knownOrderIds = [];
    try {
      // Snapshot the existing orders so the one this payment creates can be told
      // apart from orders that already existed.
      const page = await app.api.get("/api/v1/orders?category=all&limit=20");
      knownOrderIds = (page.items || []).map(item => item.id);
    } catch (error) {
      knownOrderIds = [];
    }

    try {
      const order = await app.payments.payAndConfirm({
        quoteId: this.data.quote.id,
        knownOrderIds,
      });
      createDraft.reset();
      orderNavigationIntent.set({
        category: filterForState(order.state),
        orderId: order.id,
      });
      wx.hideLoading();
      wx.switchTab({ url: "/pages/orders/index" });
    } catch (error) {
      wx.hideLoading();
      this.setData({ paying: false });

      if (error instanceof PaymentCancelled) {
        this.setData({ error: "支付已取消。" });
        return;
      }
      if (error instanceof PaymentUnconfirmed) {
        // The money may have left the user's account while the server has not
        // confirmed yet. Never claim failure, and never claim success.
        wx.showModal({
          title: "支付结果确认中",
          content: "如已完成支付，订单会稍后出现在订单列表中。请勿重复支付。",
          showCancel: false,
          success: () => {
            orderNavigationIntent.set({ category: FILTERS.GRADING });
            wx.switchTab({ url: "/pages/orders/index" });
          },
        });
        return;
      }
      this.setData({ error: error.detail || "支付失败，请重试。" });
    }
  },

  reupload() {
    createDraft.reset();
    wx.switchTab({ url: "/pages/create/upload" });
  },
});
