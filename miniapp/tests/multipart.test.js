import test from "node:test";
import assert from "node:assert/strict";

import { buildMultipartBody, createBoundary, sanitizeFilename } from "../utils/multipart.js";

function decode(buffer) {
  return new TextDecoder("utf-8").decode(new Uint8Array(buffer));
}

test("encodes the field names the quotes endpoint expects", () => {
  const boundary = "----TestBoundary";
  const { body, contentType } = buildMultipartBody({
    fields: { grading_standard: "imo", note: "第二题请重点看" },
    files: [
      { name: "source_pdf", filename: "answer.pdf", data: new Uint8Array([1, 2, 3]) },
      { name: "reference_pdf", filename: "rubric.pdf", data: new Uint8Array([4, 5]) },
    ],
    boundary,
  });

  const text = decode(body);
  assert.equal(contentType, `multipart/form-data; boundary=${boundary}`);
  assert.match(text, /name="grading_standard"/);
  assert.match(text, /name="note"/);
  assert.match(text, /name="source_pdf"; filename="answer.pdf"/);
  assert.match(text, /name="reference_pdf"; filename="rubric.pdf"/);
  assert.match(text, /Content-Type: application\/pdf/);
  assert.ok(text.endsWith(`--${boundary}--\r\n`));
});

test("preserves raw pdf bytes exactly", () => {
  const boundary = "----B";
  //0x25 0x50 0x44 0x46 is "%PDF"; a UTF-8 round trip would corrupt real
  // binary content, so the bytes are checked rather than the text.
  const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x00, 0xff, 0xfe]);
  const { body } = buildMultipartBody({
    fields: {},
    files: [{ name: "source_pdf", filename: "a.pdf", data: pdfBytes }],
    boundary,
  });

  const bytes = new Uint8Array(body);
  const start = bytes.indexOf(0x25);
  assert.deepEqual([...bytes.slice(start, start + pdfBytes.length)], [...pdfBytes]);
});

test("a filename cannot inject extra multipart headers", () => {
  const evil = 'a".pdf\r\nContent-Disposition: form-data; name="user_id"\r\n\r\nattacker';
  assert.equal(sanitizeFilename(evil).includes("\r"), false);
  assert.equal(sanitizeFilename(evil).includes('"'), false);

  const { body } = buildMultipartBody({
    fields: {},
    files: [{ name: "source_pdf", filename: evil, data: new Uint8Array([1]) }],
    boundary: "----B",
  });
  const text = decode(body);
  // The server derives ownership from the session; a smuggled user_id field
  // must not even appear in the body.
  assert.equal(text.includes('name="user_id"'), false);
});

test("omitting the optional reference file yields a single file part", () => {
  const { body } = buildMultipartBody({
    fields: { grading_standard: "cmo" },
    files: [{ name: "source_pdf", filename: "a.pdf", data: new Uint8Array([1]) }],
    boundary: "----B",
  });
  const text = decode(body);
  assert.equal(text.includes("reference_pdf"), false);
});

test("boundaries are unique per request", () => {
  assert.notEqual(createBoundary(), createBoundary());
});
