/**
 * Creation wizard state.
 *
 * A pure reducer so the rules that matter — a source PDF is mandatory, a
 * reference PDF never is, and a quote belongs to the exact files that were
 * priced — are testable without the WeChat runtime.
 *
 * The server prices the upload. Nothing here multiplies pages by a unit price.
 */

/** Matches the server's MAX_NOTE_CHARS. */
export const MAX_NOTE_CHARS = 2000;

export const initialState = Object.freeze({
  sourcePdf: null,
  referencePdf: null,
  serviceTier: "",
  standard: "",
  note: "",
  quote: null,
  uploading: false,
  progress: 0,
  error: "",
});

export function reduceCreateState(state, action) {
  switch (action.type) {
    case "SOURCE_SELECTED":
      return {
        ...state,
        sourcePdf: action.file,
        // Any quote in hand priced the previous file, so it is no longer valid.
        quote: null,
        progress: 0,
        error: "",
      };

    case "SOURCE_CLEARED":
      return { ...state, sourcePdf: null, quote: null, progress: 0, error: "" };

    case "REFERENCE_SELECTED":
      // The reference PDF is not priced, but it is part of the graded bundle,
      // so a new one still invalidates the quote it was not uploaded with.
      return { ...state, referencePdf: action.file, quote: null, error: "" };

    case "REFERENCE_CLEARED":
      return { ...state, referencePdf: null, quote: null, error: "" };

    case "STANDARD_SELECTED":
      return { ...state, standard: action.standard, quote: null, error: "" };

    case "SERVICE_TIER_SELECTED":
      return { ...state, serviceTier: action.serviceTier, quote: null, error: "" };

    case "NOTE_CHANGED":
      return { ...state, note: action.note, quote: null };

    case "UPLOAD_STARTED":
      return { ...state, uploading: true, progress: 0, error: "" };

    case "UPLOAD_PROGRESS":
      return { ...state, progress: action.progress };

    case "QUOTE_RECEIVED":
      return { ...state, quote: action.quote, uploading: false, progress: 100, error: "" };

    case "UPLOAD_FAILED":
      return { ...state, uploading: false, error: action.error, quote: null };

    case "QUOTE_EXPIRED":
      return { ...state, quote: null, error: action.error || "报价已过期，请重新上传。" };

    case "RESET":
      return { ...initialState };

    default:
      return state;
  }
}

export function noteError(note) {
  const text = note || "";
  if (text.length > MAX_NOTE_CHARS) {
    return `补充说明最多 ${MAX_NOTE_CHARS} 字，当前 ${text.length} 字。`;
  }
  return "";
}

/** A quote may be requested once a source PDF and a standard are chosen. */
export function canRequestQuote(state) {
  return (
    Boolean(state.sourcePdf) &&
    Boolean(state.serviceTier) &&
    Boolean(state.standard) &&
    !state.uploading &&
    !noteError(state.note)
  );
}
