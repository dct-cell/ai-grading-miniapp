import { createDraft } from "../../services/create-draft.js";
import { canRequestQuote, noteError, MAX_NOTE_CHARS } from "../../services/create-flow.js";
import {
  GRADING_STANDARDS,
  SERVICE_TIERS,
  formatBytes,
  formatMoney,
} from "../../utils/format.js";

const app = getApp();

/**
 * Step 2 of 3: grading standard, optional note, and the upload itself.
 *
 * POST /api/v1/quotes carries the file and the standard together, so this is
 * where bytes are in flight — and therefore where leaving the page must be
 * blocked.
 */
Page({
  data: {
    standards: GRADING_STANDARDS,
    serviceTiers: SERVICE_TIERS,
    serviceTier: "",
    standard: "",
    note: "",
    noteLength: 0,
    maxNoteChars: MAX_NOTE_CHARS,
    noteError: "",
    uploading: false,
    progress: 0,
    error: "",
    sourceName: "",
    sourceSizeText: "",
    referenceName: "",
    canSubmit: false,
    noteFocused: false,
    hasReference: false,
  },

  async onLoad() {
    const state = createDraft.getState();
    if (!state.sourcePdf) {
      // Reached without a file (e.g. a restored page stack): send the user back.
      wx.redirectTo({ url: "/pages/create/upload" });
      return;
    }
    this.setData({
      standard: state.standard,
      serviceTier: state.serviceTier,
      note: state.note,
      noteLength: (state.note || "").length,
      sourceName: state.sourcePdf.name,
      sourceSizeText: formatBytes(state.sourcePdf.size),
      referenceName: state.referencePdf ? state.referencePdf.name : "",
      hasReference: Boolean(state.referencePdf),
    });
    this.sync();
    try {
      await app.whenReady();
      const catalog = await app.quotes.listServiceTiers();
      const enabled = (catalog.items || []).filter(item => item.enabled !== false);
      if (enabled.length) {
        this.setData({
          serviceTiers: enabled.map(item => ({
            value: item.id,
            label: item.label,
            hint: `${item.description} · ${formatMoney(item.cents_per_page)}/页`,
          })),
        });
      }
    } catch (_error) {
      // The static labels keep the already-loaded page stable. Quote creation
      // remains server-authoritative and will reject a disabled tier.
    }
  },

  onUnload() {
    this.releaseGuard();
  },

  sync() {
    const state = createDraft.getState();
    this.setData({
      uploading: state.uploading,
      progress: state.progress,
      error: state.error,
      noteError: noteError(state.note),
      canSubmit: canRequestQuote(state),
    });
  },

  dispatch(action) {
    createDraft.dispatch(action);
    this.sync();
  },

  chooseStandard(event) {
    if (createDraft.getState().uploading) {
      return;
    }
    this.dispatch({ type: "STANDARD_SELECTED", standard: event.detail.value });
    this.setData({ standard: createDraft.getState().standard });
  },

  chooseServiceTier(event) {
    if (createDraft.getState().uploading) {
      return;
    }
    this.dispatch({ type: "SERVICE_TIER_SELECTED", serviceTier: event.detail.value });
    this.setData({ serviceTier: createDraft.getState().serviceTier });
  },

  onNoteInput(event) {
    const note = event.detail.value;
    this.dispatch({ type: "NOTE_CHANGED", note });
    this.setData({ note, noteLength: note.length });
  },

  onNoteFocus() {
    this.setData({ noteFocused: true });
  },

  onNoteBlur() {
    this.setData({ noteFocused: false });
  },

  applyGuard() {
    if (typeof wx.enableAlertBeforeUnload === "function") {
      wx.enableAlertBeforeUnload({
        message: "文件正在上传，离开将中断上传。确定离开？",
      });
    }
    this.guarded = true;
  },

  releaseGuard() {
    if (this.guarded && typeof wx.disableAlertBeforeUnload === "function") {
      wx.disableAlertBeforeUnload();
    }
    this.guarded = false;
  },

  /** Block the hardware/gesture back action while bytes are moving. */
  onBackPress() {
    return createDraft.getState().uploading;
  },

  async submit() {
    if (!canRequestQuote(createDraft.getState())) {
      return;
    }
    const state = createDraft.getState();
    this.dispatch({ type: "UPLOAD_STARTED" });
    this.applyGuard();
    wx.showLoading({ title: "上传中", mask: true });

    try {
      // The quote service picks the transport: wx.uploadFile for a lone source
      // PDF, or one encoded multipart request when a reference PDF is included.
      const quote = await app.quotes.create({
        sourcePdf: state.sourcePdf,
        referencePdf: state.referencePdf,
        serviceTier: state.serviceTier,
        gradingStandard: state.standard,
        note: state.note,
        onProgress: event => {
          if (state.referencePdf) {
            return;
          }
          const progress = Number(event && event.progress);
          if (Number.isFinite(progress)) {
            this.dispatch({ type: "UPLOAD_PROGRESS", progress: Math.max(0, Math.min(100, progress)) });
          }
        },
      });
      this.dispatch({ type: "QUOTE_RECEIVED", quote });
      wx.navigateTo({ url: "/pages/create/payment" });
    } catch (error) {
      this.dispatch({
        type: "UPLOAD_FAILED",
        error: error.detail || "上传失败，请重试。",
      });
    } finally {
      wx.hideLoading();
      this.releaseGuard();
    }
  },
});
