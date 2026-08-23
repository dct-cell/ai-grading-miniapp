/**
 * Quote creation.
 *
 * Transport depends on how many files there are, because `wx.uploadFile` can
 * only send one:
 *
 *   source only          -> wx.uploadFile (streams from disk, reports progress)
 *   source + reference   -> read both, encode multipart, wx.request
 *
 * The single-file path is preferred whenever possible: it streams the file
 * instead of loading it into memory and it gives real upload progress.
 *
 * The server prices the result. This module never computes an amount.
 */
import { buildMultipartBody, createBoundary } from "../utils/multipart.js";

export const QUOTES_PATH = "/api/v1/quotes";

export function createQuoteService({ api, readFile, boundaryFactory = createBoundary }) {
  return {
    /**
     * @param {object} options
     * @param {{path:string,name:string}} options.sourcePdf
     * @param {{path:string,name:string}|null} options.referencePdf
     * @param {string} options.gradingStandard
     * @param {string} options.note
     */
    async create({ sourcePdf, referencePdf, serviceTier, gradingStandard, note, onProgress }) {
      const fields = {
        service_tier: serviceTier,
        grading_standard: gradingStandard,
        note: note || "",
      };

      if (!referencePdf) {
        return api.uploadPdf({
          path: QUOTES_PATH,
          filePath: sourcePdf.path,
          name: "source_pdf",
          formData: fields,
          onProgress,
        });
      }

      const [sourceBytes, referenceBytes] = await Promise.all([
        readFile(sourcePdf.path),
        readFile(referencePdf.path),
      ]);
      const { body, contentType } = buildMultipartBody({
        fields,
        files: [
          { name: "source_pdf", filename: sourcePdf.name, data: sourceBytes },
          { name: "reference_pdf", filename: referencePdf.name, data: referenceBytes },
        ],
        boundary: boundaryFactory(),
      });
      return api.postMultipart({ path: QUOTES_PATH, body, contentType });
    },

    get(quoteId) {
      return api.get(`${QUOTES_PATH}/${quoteId}`);
    },

    listServiceTiers() {
      return api.get("/api/v1/service-tiers");
    },
  };
}
