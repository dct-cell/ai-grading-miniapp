/**
 * Order list pagination and detail polling.
 *
 * The poller is a plain object with explicit start/stop so a page can guarantee
 * it is stopped in both `onHide` and `onUnload`. It stores the timer id and
 * clears it — flipping a boolean without clearing the timer would leave the
 * callback scheduled and keep hitting the server after the page is gone.
 */
import { FILTERS, isActive } from "../utils/order-states.js";

export const POLL_INTERVAL_MS = 15_000;

export function createOrderService({ api }) {
  return {
    /**
     * One page of the user's orders.
     *
     * `category` is passed to the server, which owns the grouping. A null
     * `next_cursor` means the end; the caller must stop paging there.
     */
    async list({ category = FILTERS.ALL, cursor = null, limit = 20 } = {}) {
      const params = [`category=${encodeURIComponent(category)}`, `limit=${limit}`];
      if (cursor) {
        params.push(`cursor=${encodeURIComponent(cursor)}`);
      }
      const page = await api.get(`/api/v1/orders?${params.join("&")}`);
      return {
        items: (page && page.items) || [],
        nextCursor: (page && page.next_cursor) || null,
      };
    },

    get(orderId) {
      return api.get(`/api/v1/orders/${orderId}`);
    },

    progress(orderIds) {
      const ids = Array.from(new Set(orderIds || [])).slice(0, 50);
      if (ids.length === 0) {
        return Promise.resolve({ items: [] });
      }
      const query = ids
        .map(orderId => `order_ids=${encodeURIComponent(orderId)}`)
        .join("&");
      return api.get(`/api/v1/orders/progress?${query}`);
    },
  };
}

/**
 * A visibility-scoped poller for one order.
 *
 * `setTimeout` is re-armed after each response rather than using setInterval,
 * so a slow response cannot stack up overlapping requests.
 */
export function createOrderPoller({
  fetchOrder,
  onUpdate,
  onError,
  intervalMs = POLL_INTERVAL_MS,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  let timerId = null;
  let running = false;

  function schedule() {
    if (!running) {
      return;
    }
    timerId = setTimer(tick, intervalMs);
  }

  async function tick() {
    timerId = null;
    if (!running) {
      return;
    }
    try {
      const order = await fetchOrder();
      // A stop() during the in-flight request must not deliver an update to a
      // page that has already been unloaded.
      if (!running) {
        return;
      }
      onUpdate(order);
      // Stop as soon as there is nothing left to watch, so a delivered order
      // does not keep polling forever.
      if (!isActive(order.state)) {
        stop();
        return;
      }
    } catch (error) {
      if (!running) {
        return;
      }
      if (typeof onError === "function") {
        onError(error);
      }
    }
    schedule();
  }

  function start() {
    if (running) {
      return;
    }
    running = true;
    schedule();
  }

  function stop() {
    running = false;
    if (timerId !== null) {
      // Clearing the timer is the point: leaving it armed would keep polling
      // after onHide/onUnload.
      clearTimer(timerId);
      timerId = null;
    }
  }

  return {
    start,
    stop,
    isRunning: () => running,
    hasPendingTimer: () => timerId !== null,
  };
}

/** A visibility-scoped polling loop for the compact task-list status payload. */
export function createOrderProgressPoller({
  fetchProgress,
  onUpdate,
  onError,
  intervalMs = POLL_INTERVAL_MS,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  let timerId = null;
  let running = false;

  function schedule() {
    if (running) {
      timerId = setTimer(tick, intervalMs);
    }
  }

  async function tick() {
    timerId = null;
    if (!running) {
      return;
    }
    try {
      const progress = await fetchProgress();
      if (!running) {
        return;
      }
      onUpdate(progress);
    } catch (error) {
      if (!running) {
        return;
      }
      if (typeof onError === "function") {
        onError(error);
      }
    }
    schedule();
  }

  function start() {
    if (running) {
      return;
    }
    running = true;
    schedule();
  }

  function stop() {
    running = false;
    if (timerId !== null) {
      clearTimer(timerId);
      timerId = null;
    }
  }

  return {
    start,
    stop,
    isRunning: () => running,
    hasPendingTimer: () => timerId !== null,
  };
}
