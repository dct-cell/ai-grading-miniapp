/**
 * Payment.
 *
 * THE invariant of this module: a payment UI success callback is not proof of
 * payment. Only a server-verified callback creates an order. So every path
 * here ends the same way — poll the server's own order list until the order
 * exists, and report `PaymentUnconfirmed` if it never appears.
 *
 * Two environment paths, both real:
 *   staging     prepay -> POST simulate-success (verified callback service)
 *   production  prepay -> wx.requestPayment -> wait for WeChat's callback
 *
 * `wx.requestPayment` resolving only means the user finished the payment UI. The
 * money may still fail, and the server may not have been notified yet. Treating
 * that callback as "paid" would let a user reach a result page for an order
 * that does not exist.
 */
import { ApiError } from "./api.js";
import { usesSimulatedPayment } from "../config.js";

const PREPAY_PATH = "/api/v1/payments/prepay";

/** The payment was not confirmed by the server within the polling window. */
export class PaymentUnconfirmed extends Error {
  constructor(paymentId) {
    super("支付结果确认中，请稍后在订单列表查看。");
    this.name = "PaymentUnconfirmed";
    this.paymentId = paymentId;
  }
}

/** The user dismissed the payment sheet. */
export class PaymentCancelled extends Error {
  constructor() {
    super("支付已取消。");
    this.name = "PaymentCancelled";
  }
}

const DEFAULT_MAX_ATTEMPTS = 10;
const DEFAULT_INTERVAL_MS = 1_500;

export function createPaymentFlow({
  api,
  profile,
  requestPayment,
  wait = ms => new Promise(resolve => setTimeout(resolve, ms)),
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
  intervalMs = DEFAULT_INTERVAL_MS,
}) {
  /**
   * Find the order this payment created.
   *
   * Identified by *absence before payment*: the server has no
   * "order for quote" lookup, and adding one is out of scope for this phase.
   * Comparing against the ids the caller already knew avoids mistaking an
   * existing order for the new one.
   */
  async function findNewOrder(knownOrderIds) {
    const known = new Set(knownOrderIds || []);
    const page = await api.get("/api/v1/orders?category=all&limit=20");
    const items = (page && page.items) || [];
    return items.find(item => !known.has(item.id)) || null;
  }

  async function pollForOrder(knownOrderIds, paymentId) {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      // Server state is the only evidence that counts.
      const order = await findNewOrder(knownOrderIds);
      if (order) {
        return order;
      }
      if (attempt < maxAttempts - 1) {
        await wait(intervalMs);
      }
    }
    throw new PaymentUnconfirmed(paymentId);
  }

  return {
    /** Create the payment intent. The amount comes from the server. */
    prepay(quoteId) {
      return api.post(PREPAY_PATH, { quote_id: quoteId });
    },

    async payAndConfirm({ quoteId, knownOrderIds }) {
      const intent = await this.prepay(quoteId);

      if (usesSimulatedPayment(profile)) {
        // Not registered in production; drives the same verified callback
        // service a real gateway callback would.
        await api.post(`/api/v1/payments/${intent.payment_id}/simulate-success`);
      } else {
        if (typeof requestPayment !== "function") {
          throw new Error("wx.requestPayment is required in production");
        }
        try {
          await requestPayment(intent.client_payload);
        } catch (error) {
          const message = (error && (error.errMsg || error.message)) || "";
          if (message.includes("cancel")) {
            throw new PaymentCancelled();
          }
          throw new ApiError(0, "支付未完成，请重试。");
        }
        // Deliberately no early return here: the resolved callback proves
        // nothing about the server's view of the payment.
      }

      return pollForOrder(knownOrderIds, intent.payment_id);
    },
  };
}
