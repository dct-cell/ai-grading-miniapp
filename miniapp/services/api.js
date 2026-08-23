/**
 * The single HTTP seam between the mini-program and the server.
 *
 * Two design rules matter here:
 *
 * 1. `request` and `upload` are injected. In the WeChat runtime `app.js`
 *    passes `wx.request` / `wx.uploadFile`; in tests a stub is passed. That
 *    keeps every consumer of this module testable under plain Node, where no
 *    `wx` global exists.
 * 2. Errors are normalized to `ApiError(status, detail)` and the `detail`
 *    string is carried through verbatim. The server authors every
 *    user-visible failure message, so inventing client-side copy here would
 *    let the UI contradict the server.
 */

export const REQUEST_TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const GENERIC_DETAIL = "请求失败，请稍后重试。";
const NETWORK_DETAIL = "网络连接失败，请检查网络后重试。";

function detailFrom(body, status) {
  if (body && typeof body === "object" && typeof body.detail === "string") {
    return body.detail;
  }
  // FastAPI validation errors arrive as a list of objects under `detail`.
  if (body && typeof body === "object" && Array.isArray(body.detail)) {
    const first = body.detail[0];
    if (first && typeof first.msg === "string") {
      return first.msg;
    }
  }
  if (typeof body === "string" && body.trim() !== "") {
    return body;
  }
  return `${GENERIC_DETAIL}(${status})`;
}

export function createApiClient({
  baseUrl,
  getToken,
  request,
  upload,
  onUnauthorized,
}) {
  if (typeof request !== "function") {
    throw new TypeError("createApiClient requires a request function");
  }

  const trimmedBase = String(baseUrl || "").replace(/\/+$/, "");

  function headersFor(extra) {
    const header = { ...extra };
    const token = typeof getToken === "function" ? getToken() : null;
    // Only attach the header when a token actually exists. Sending
    // "Bearer null" would turn an intentionally anonymous call (login) into a
    // rejected one.
    if (token) {
      header.Authorization = `Bearer ${token}`;
    }
    return header;
  }

  function urlFor(path) {
    return `${trimmedBase}${path}`;
  }

  function handleFailure(status, detail) {
    //401 means the session itself is gone, so the app re-authenticates.
    // 403/410 are authorisation decisions about one resource (a revoked
    // download, for example) and must not log the user out.
    if (status === 401 && typeof onUnauthorized === "function") {
      onUnauthorized();
    }
    throw new ApiError(status, detail);
  }

  async function send(method, path, data, { header } = {}) {
    let response;
    try {
      response = await request({
        url: urlFor(path),
        method,
        data,
        header: headersFor(header),
        timeout: REQUEST_TIMEOUT_MS,
      });
    } catch (error) {
      // wx.request rejects on transport failures and timeouts alike; neither
      // carries an HTTP status, so status 0 marks "never reached the server".
      throw new ApiError(0, NETWORK_DETAIL);
    }

    const status = response ? response.statusCode : 0;
    if (status < 200 || status >= 300) {
      handleFailure(status, detailFrom(response && response.data, status));
    }
    return response.data;
  }

  return {
    get(path) {
      return send("GET", path, undefined);
    },

    post(path, data, options) {
      return send(
        "POST",
        path,
        data === undefined ? {} : data,
        {
          header: { "content-type": "application/json", ...(options && options.header) },
        },
      );
    },

    /**
     * Upload one PDF as multipart/form-data.
     *
     * `wx.uploadFile` differs from `wx.request` in a way that has bitten this
     * flow before: it resolves with `data` as a raw *string*, never parsed
     * JSON. Parsing failures are surfaced as ApiError rather than crashing the
     * page, because a proxy error page is a realistic response here.
     */
    async uploadPdf({ path, filePath, name, formData, onProgress }) {
      if (typeof upload !== "function") {
        throw new TypeError("uploadPdf requires an upload function");
      }
      let response;
      try {
        response = await upload({
          url: urlFor(path),
          filePath,
          name,
          formData,
          header: headersFor(),
          timeout: REQUEST_TIMEOUT_MS,
        }, onProgress);
      } catch (error) {
        throw new ApiError(0, NETWORK_DETAIL);
      }

      const status = response ? response.statusCode : 0;
      let body;
      try {
        body = JSON.parse(response.data);
      } catch (error) {
        if (status < 200 || status >= 300) {
          handleFailure(status, detailFrom(response && response.data, status));
        }
        throw new ApiError(status, GENERIC_DETAIL);
      }

      if (status < 200 || status >= 300) {
        handleFailure(status, detailFrom(body, status));
      }
      return body;
    },

    /**
     * POST a pre-encoded multipart body through wx.request.
     *
     * Needed when two files must travel in one request (source + reference),
     * which wx.uploadFile cannot express.
     */
    async postMultipart({ path, body, contentType }) {
      let response;
      try {
        response = await request({
          url: urlFor(path),
          method: "POST",
          data: body,
          header: headersFor({ "content-type": contentType }),
          timeout: REQUEST_TIMEOUT_MS,
        });
      } catch (error) {
        throw new ApiError(0, NETWORK_DETAIL);
      }

      const status = response ? response.statusCode : 0;
      let payload = response.data;
      if (typeof payload === "string") {
        try {
          payload = JSON.parse(payload);
        } catch (error) {
          payload = null;
        }
      }
      if (status < 200 || status >= 300) {
        handleFailure(status, detailFrom(payload || response.data, status));
      }
      return payload;
    },
  };
}
