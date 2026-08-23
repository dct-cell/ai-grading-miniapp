import test from "node:test";
import assert from "node:assert/strict";

import { createQuoteService, QUOTES_PATH } from "../services/quotes.js";

test("a source-only quote streams through wx.uploadFile", async () => {
  const uploads = [];
  const service = createQuoteService({
    api: {
      uploadPdf: async options => {
        uploads.push(options);
        return { id: "q1", page_count: 3, amount_cents: 3000 };
      },
      postMultipart: async () => {
        throw new Error("must not encode a body for a single file");
      },
    },
    readFile: async () => {
      throw new Error("must not read the file into memory");
    },
  });

  const quote = await service.create({
    sourcePdf: { path: "wxfile://s.pdf", name: "s.pdf" },
    referencePdf: null,
    gradingStandard: "imo",
    note: "",
  });

  assert.equal(quote.amount_cents, 3000);
  assert.equal(uploads[0].path, QUOTES_PATH);
  assert.equal(uploads[0].name, "source_pdf");
  assert.equal(uploads[0].formData.grading_standard, "imo");
});

test("both pdfs travel in one multipart request", async () => {
  const posted = [];
  const service = createQuoteService({
    api: {
      uploadPdf: async () => {
        throw new Error("wx.uploadFile cannot carry two files");
      },
      postMultipart: async options => {
        posted.push(options);
        return { id: "q2", page_count: 5, amount_cents: 5000 };
      },
    },
    readFile: async path =>
      path.includes("ref") ? new Uint8Array([9, 9]) : new Uint8Array([1, 2, 3]),
    boundaryFactory: () => "----Fixed",
  });

  const quote = await service.create({
    sourcePdf: { path: "wxfile://s.pdf", name: "s.pdf" },
    referencePdf: { path: "wxfile://ref.pdf", name: "ref.pdf" },
    gradingStandard: "cmo",
    note: "n",
  });

  assert.equal(quote.id, "q2");
  const text = new TextDecoder().decode(new Uint8Array(posted[0].body));
  assert.match(text, /name="source_pdf"/);
  assert.match(text, /name="reference_pdf"/);
  assert.equal(posted[0].contentType, "multipart/form-data; boundary=----Fixed");
});

test("the note is always sent, even when empty", async () => {
  const uploads = [];
  const service = createQuoteService({
    api: {
      uploadPdf: async options => {
        uploads.push(options);
        return {};
      },
    },
    readFile: async () => new Uint8Array(),
  });

  await service.create({
    sourcePdf: { path: "p", name: "n.pdf" },
    referencePdf: null,
    gradingStandard: "imo",
    note: undefined,
  });

  assert.equal(uploads[0].formData.note, "");
});

test("single-file quotes forward real upload progress without changing the request", async () => {
  const events = [];
  const service = createQuoteService({
    api: {
      uploadPdf: async options => {
        options.onProgress({ progress: 37 });
        return { id: "q-progress" };
      },
    },
    readFile: async () => new Uint8Array(),
  });

  await service.create({
    sourcePdf: { path: "wxfile://source.pdf", name: "source.pdf" },
    referencePdf: null,
    gradingStandard: "imo",
    note: "",
    onProgress: event => events.push(event.progress),
  });

  assert.deepEqual(events, [37]);
});
