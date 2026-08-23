/**
 * Turn server order payloads into view models.
 *
 * Only formatting and label lookup happen here. Nothing computes price,
 * eligibility or state — those all come from the server.
 */
import { formatCents, formatDateTime, formatEta, serviceTierLabel, standardLabel } from "./format.js";
import { displayLabel, stateTone } from "./order-states.js";

export function decorateSummary(order) {
  return {
    ...order,
    stateText: displayLabel(order.state, order.rounds),
    tone: stateTone(order.state),
    standardText: standardLabel(order.grading_standard),
    serviceTierText: serviceTierLabel(order.service_tier),
    amountText: formatCents(order.paid_amount_cents),
    createdText: formatDateTime(order.created_at),
    etaText: formatEta(order.eta),
  };
}
