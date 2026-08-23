import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

/**
 * jsdom here provides `window` and `document` but not web storage, and Node's
 * own experimental `localStorage` is unavailable without `--localstorage-file`.
 * Installing a minimal in-memory implementation means the "no credential is
 * persisted" assertions test something real: if the SPA ever wrote a token,
 * these objects would record it and the assertion would fail.
 */
class MemoryStorage implements Storage {
  #entries = new Map<string, string>();

  get length(): number {
    return this.#entries.size;
  }

  clear(): void {
    this.#entries.clear();
  }

  getItem(key: string): string | null {
    return this.#entries.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.#entries.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.#entries.delete(key);
  }

  setItem(key: string, value: string): void {
    this.#entries.set(key, String(value));
    // Mirror onto the instance so `Object.keys(storage)` sees it, which is how
    // the tests check that nothing was persisted.
    Object.defineProperty(this, key, {
      value: String(value),
      configurable: true,
      enumerable: true,
      writable: true,
    });
  }
}

for (const name of ["localStorage", "sessionStorage"] as const) {
  if (typeof globalThis[name] === "undefined") {
    Object.defineProperty(globalThis, name, {
      value: new MemoryStorage(),
      configurable: true,
      writable: true,
    });
  }
}

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});
