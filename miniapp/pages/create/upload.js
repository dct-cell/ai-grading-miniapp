import { MAX_NOTE_CHARS } from "../../services/create-flow.js";
import { createDraft } from "../../services/create-draft.js";
import { formatBytes } from "../../utils/format.js";

/**
 * Step 1 of 3: choose the PDFs.
 *
 * Only selection happens here. The bytes are uploaded on the options page,
 * because POST /api/v1/quotes needs the file and the grading standard in the
 * same multipart request — so the "do not leave while uploading" guard lives
 * there, where bytes are actually in flight.
 */

Page({
  data: {
    sourcePdf: null,
    referencePdf: null,
    error: "",
    sourceSizeText: "",
    referenceSizeText: "",
    maxNoteChars: MAX_NOTE_CHARS,
    canContinue: false,
  },

  onLoad() {
    createDraft.reset();
    this.sync();
  },

  onShow() {
    this.sync();
  },

  sync() {
    const state = createDraft.getState();
    this.setData({
      sourcePdf: state.sourcePdf,
      referencePdf: state.referencePdf,
      error: state.error,
      sourceSizeText: state.sourcePdf ? formatBytes(state.sourcePdf.size) : "",
      referenceSizeText: state.referencePdf ? formatBytes(state.referencePdf.size) : "",
      canContinue: Boolean(state.sourcePdf),
    });
  },

  dispatch(action) {
    createDraft.dispatch(action);
    this.sync();
  },

  pickPdf(kind) {
    wx.chooseMessageFile({
      count: 1,
      type: "file",
      extension: ["pdf"],
      success: result => {
        const file = result.tempFiles[0];
        if (!file) {
          return;
        }
        if (!/\.pdf$/i.test(file.name)) {
          this.dispatch({ type: "UPLOAD_FAILED", error: "只支持 PDF 文件。" });
          return;
        }
        this.dispatch({
          type: kind === "source" ? "SOURCE_SELECTED" : "REFERENCE_SELECTED",
          file: { path: file.path, name: file.name, size: file.size },
        });
      },
      fail: () => {
        /* The user dismissed the picker; nothing to report. */
      },
    });
  },

  chooseSource() {
    this.pickPdf("source");
  },

  chooseReference() {
    this.pickPdf("reference");
  },

  clearSource() {
    this.dispatch({ type: "SOURCE_CLEARED" });
  },

  clearReference() {
    this.dispatch({ type: "REFERENCE_CLEARED" });
  },

  goOptions() {
    if (!this.data.canContinue) {
      return;
    }
    wx.navigateTo({ url: "/pages/create/options" });
  },
});
