/**
 * Build script: bundles src/widget.js -> one IIFE string, inlines it (plus
 * src/styles.css) into src/widget.html, writes the result to dist/widget.html.
 *
 * MCP App resources must be a single self-contained HTML document (the host
 * renders it in a sandboxed iframe, no external <script src>/<link href>) --
 * see docs/RESEARCH.md "Interactive product search widget (MCP Apps)".
 *
 * pc_express_mcp/interactive_search.py reads dist/widget.html as a static
 * file at import time. Re-run `npm run build` after editing anything in
 * src/, then commit the updated dist/widget.html -- the Python server does
 * not run Node at runtime.
 */

import esbuild from "esbuild";
import fs from "node:fs/promises";
import path from "node:path";

const root = path.dirname(new URL(import.meta.url).pathname);
const dist = path.join(root, "dist");
await fs.mkdir(dist, { recursive: true });

const bundle = await esbuild.build({
  entryPoints: [path.join(root, "src/widget.js")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2022",
  minify: false, // keep it readable/diffable in git
  write: false,
});
const widgetJs = bundle.outputFiles[0].text;
const css = await fs.readFile(path.join(root, "src/styles.css"), "utf-8");
const shell = await fs.readFile(path.join(root, "src/widget.html"), "utf-8");

// Function replacements so `$` sequences in the CSS/JS aren't interpreted as
// String.replace special patterns ($&, $1, $$).
const html = shell
  .replace("/* INLINE_CSS */", () => css)
  .replace("// INLINE_JS", () => widgetJs);

await fs.writeFile(path.join(dist, "widget.html"), html, "utf-8");
console.log(`built -> ${path.join(dist, "widget.html")} (${html.length} bytes)`);
