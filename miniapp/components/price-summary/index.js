/**
 * Price summary.
 *
 * Purely presentational: every value is a string already formatted from the
 * server's quote. The component performs no arithmetic, so it cannot disagree
 * with the amount the server will charge.
 */
Component({
  properties: {
    pageCount: { type: Number, value: 0 },
    unitPriceText: { type: String, value: "" },
    totalText: { type: String, value: "" },
    expiresInText: { type: String, value: "" },
  },
});
