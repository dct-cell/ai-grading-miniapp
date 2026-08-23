import { FILTERS } from "../utils/order-states.js";

const VALID_CATEGORIES = new Set(Object.values(FILTERS));

/**
 * One-shot navigation state used when a tab page must become the real parent of
 * a non-tab detail page. Keeping this in memory avoids stale persisted routes:
 * once the orders tab consumes the intent, normal back navigation takes over.
 */
export function createOrderNavigationIntent() {
  let pending = null;

  return {
    set({ category = FILTERS.ALL, orderId = "" } = {}) {
      pending = {
        category: VALID_CATEGORIES.has(category) ? category : FILTERS.ALL,
        orderId: typeof orderId === "string" ? orderId : "",
      };
    },

    consume() {
      const intent = pending;
      pending = null;
      return intent;
    },

    clear() {
      pending = null;
    },
  };
}

export const orderNavigationIntent = createOrderNavigationIntent();
