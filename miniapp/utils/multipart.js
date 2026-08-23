/**
 * multipart/form-data encoder.
 *
 * Why this exists: `wx.uploadFile` can send exactly one file, but
 * POST /api/v1/quotes accepts `source_pdf` plus an optional `reference_pdf` in
 * a single request. When both PDFs are present the body is therefore assembled
 * here and sent with `wx.request`, which accepts an ArrayBuffer body.
 *
 * The field names must match the server's Form/File parameters exactly:
 * source_pdf, reference_pdf, grading_standard, note.
 *
 * Kept pure (ArrayBuffers in, ArrayBuffer out) so it is testable under plain
 * Node without the WeChat runtime.
 */

const CRLF = "\r\n";

/** A boundary that cannot appear in the encoded payload. */
export function createBoundary(random = Math.random) {
  const suffix = random().toString(36).slice(2, 12);
  return `----GraderFormBoundary${suffix}`;
}

function encodeUtf8(text) {
  return new TextEncoder().encode(text);
}

function concatBytes(chunks) {
  const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const output = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output;
}

/**
 * Build a multipart body.
 *
 * @param {object} options
 * @param {Record<string,string>} options.fields plain text fields
 * @param {Array<{name:string,filename:string,data:ArrayBuffer|Uint8Array}>} options.files
 * @param {string} options.boundary
 * @returns {{contentType:string, body:ArrayBuffer}}
 */
export function buildMultipartBody({ fields = {}, files = [], boundary }) {
  if (!boundary) {
    throw new TypeError("buildMultipartBody requires a boundary");
  }
  const chunks = [];

  for (const [name, value] of Object.entries(fields)) {
    chunks.push(
      encodeUtf8(
        `--${boundary}${CRLF}` +
          `Content-Disposition: form-data; name="${name}"${CRLF}${CRLF}` +
          `${value === undefined || value === null ? "" : value}${CRLF}`,
      ),
    );
  }

  for (const file of files) {
    const bytes = file.data instanceof Uint8Array ? file.data : new Uint8Array(file.data);
    chunks.push(
      encodeUtf8(
        `--${boundary}${CRLF}` +
          `Content-Disposition: form-data; name="${file.name}"; ` +
          `filename="${sanitizeFilename(file.filename)}"${CRLF}` +
          `Content-Type: application/pdf${CRLF}${CRLF}`,
      ),
    );
    chunks.push(bytes);
    chunks.push(encodeUtf8(CRLF));
  }

  chunks.push(encodeUtf8(`--${boundary}--${CRLF}`));

  const body = concatBytes(chunks);
  return {
    contentType: `multipart/form-data; boundary=${boundary}`,
    body: body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength),
  };
}

/**
 * Strip quotes, CR and LF from a filename.
 *
 * A filename is user-controlled input; letting a quote or a newline through
 * would let it terminate the header early and inject extra multipart headers.
 */
export function sanitizeFilename(filename) {
  return String(filename || "upload.pdf").replace(/[\r\n"\\]/g, "_");
}
