import { initialState, reduceCreateState } from "./create-flow.js";

/**
 * One in-memory draft shared by the three creation pages.
 *
 * This module deliberately has no runtime page-registration side effects.
 * Keeping the draft outside a page module prevents importing a second definition when
 * the wizard moves from file selection to options or payment.
 */
let state = { ...initialState };

export const createDraft = {
  getState() {
    return state;
  },

  dispatch(action) {
    state = reduceCreateState(state, action);
    return state;
  },

  reset() {
    state = { ...initialState };
    return state;
  },
};
