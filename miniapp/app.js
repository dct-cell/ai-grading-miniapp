/**
 * App entry point.
 *
 * Responsibilities kept deliberately narrow: build the one API client, wire it
 * to wx transport functions, restore or establish a session, and expose both to
 * pages via getApp(). Pages never construct their own client, so the auth
 * header and the 401 handling exist in exactly one place.
 */
import { createApiClient } from "./services/api.js";
import { createSessionStore, createWxStorage } from "./services/session.js";
import { createAuthService } from "./services/auth.js";
import { createQuoteService } from "./services/quotes.js";
import { createOrderService } from "./services/orders.js";
import { createAftersalesService } from "./services/aftersales.js";
import { createPaymentFlow } from "./services/payments.js";
import { createDownloadService } from "./services/downloads.js";
import { resolveLaunchProfile } from "./config.js";

function promisify(wxMethod) {
  return options =>
    new Promise((resolve, reject) => {
      wxMethod({ ...options, success: resolve, fail: reject });
    });
}

/** wx.uploadFile returns an UploadTask, so retain it for real progress events. */
function uploadFile(options, onProgress) {
  return new Promise((resolve, reject) => {
    const task = wx.uploadFile({ ...options, success: resolve, fail: reject });
    if (task && typeof task.onProgressUpdate === "function" && typeof onProgress === "function") {
      task.onProgressUpdate(onProgress);
    }
  });
}

/** Read a local temp file as an ArrayBuffer (needed for two-file uploads). */
function readLocalFile(path) {
  return new Promise((resolve, reject) => {
    wx.getFileSystemManager().readFile({
      filePath: path,
      success: result => resolve(result.data),
      fail: reject,
    });
  });
}

App({
  globalData: {
    profile: null,
    user: null,
    ready: false,
  },

  onLaunch(options) {
    const profile = resolveLaunchProfile(options);
    this.globalData.profile = profile;

    const sessions = createSessionStore({ storage: createWxStorage(wx) });
    this.sessions = sessions;

    this.api = createApiClient({
      baseUrl: profile.baseUrl,
      getToken: () => sessions.getToken(),
      request: promisify(wx.request),
      upload: uploadFile,
      // A dead session is dropped immediately so the next call re-authenticates
      // rather than retrying with a token the server has already rejected.
      onUnauthorized: () => sessions.clear(),
    });

    this.auth = createAuthService({
      api: this.api,
      sessions,
      profile,
      login: promisify(wx.login),
    });

    this.quotes = createQuoteService({
      api: this.api,
      readFile: readLocalFile,
    });

    this.orders = createOrderService({ api: this.api });

    this.aftersales = createAftersalesService({ api: this.api });

    this.payments = createPaymentFlow({
      api: this.api,
      profile,
      requestPayment: promisify(wx.requestPayment),
    });

    this.downloads = createDownloadService({
      api: this.api,
      baseUrl: profile.baseUrl,
      getToken: () => sessions.getToken(),
      downloadFile: promisify(wx.downloadFile),
      openDocument: promisify(wx.openDocument),
    });

    this.readyPromise = this.auth
      .ensureLogin()
      .then(user => {
        this.globalData.user = user;
        this.globalData.ready = true;
        return user;
      })
      .catch(error => {
        // Surfaced by whichever page awaits readyPromise; a launch-time modal
        // here would fire before any page can render.
        this.globalData.ready = false;
        throw error;
      });
  },

  /** Pages await this before their first authenticated request. */
  whenReady() {
    return this.readyPromise;
  },
});
