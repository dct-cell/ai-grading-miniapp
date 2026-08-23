/// <reference types="vite/client" />

// Vite resolves a bare CSS import as a side effect; TypeScript needs to be told
// the module exists so `import "./styles/tokens.css"` type-checks.
declare module "*.css";
