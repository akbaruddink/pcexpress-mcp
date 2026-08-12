/**
 * PC Express interactive product search widget.
 *
 * Two modes, one render path (mirrors the pattern documented in
 * https://github.com/iamneilroberts/mcp-apps-interactive-ui, which this
 * project verified against before building this):
 *
 *   - In a host (claude.ai / Desktop): connects over the postMessage
 *     bridge, receives the launcher tool's small { result_ref, query,
 *     count } via ontoolresult, fetches the full product list itself via
 *     the app-only _interactive_search_results tool (keeps the payload
 *     out of the model's context -- see interactive_search.py), and calls
 *     the real add_to_cart tool directly when a card's Add button is
 *     tapped.
 *
 *   - Standalone (opened directly in a browser, no host): renders
 *     embedded mock data so the widget can be previewed without a
 *     running server. Add buttons no-op with a console note.
 */

import { App, applyDocumentTheme, applyHostFonts, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";

const MOCK_RESULTS = [
  {
    code: "MOCK_EA",
    name: "Standalone Preview Product",
    package_size: "1 ea",
    price: 4.99,
    regular_price: null,
    photo_markdown: null,
    image_urls: [],
  },
];

const inHost = window.parent && window.parent !== window;

const root = document.getElementById("root");
/** @type {import("@modelcontextprotocol/ext-apps").App | null} */
let app = null;
let query = "";
let results = MOCK_RESULTS;
const addedCodes = new Set();

function caps() {
  return app?.getHostCapabilities() ?? {};
}
const can = {
  serverTools: () => !!caps().serverTools,
  message: () => !!caps().message,
  modelContext: () => !!caps().updateModelContext,
};

function log(level, data) {
  if (inHost && caps().logging) app?.sendLog({ level, data });
}

// ---- parsing tool results ----
// This project's Python tools return plain dicts (structuredContent), with
// a JSON-stringified text content block as a fallback -- try both rather
// than assuming which one a given host/SDK version actually populates.
function extractToolData(res) {
  if (res?.structuredContent && typeof res.structuredContent === "object") {
    return res.structuredContent;
  }
  const textBlock = res?.content?.find((c) => c.type === "text");
  if (textBlock?.text) {
    try {
      return JSON.parse(textBlock.text);
    } catch {
      return { error: "unparseable", message: textBlock.text };
    }
  }
  return null;
}

// ---- render ----
function render() {
  root.innerHTML = "";

  if (!inHost) {
    root.appendChild(el("div", "standalone-note", "Standalone preview (no MCP host). Add buttons no-op."));
  }

  const head = el("div", "head");
  head.appendChild(el("h1", "", query ? `Results for "${query}"` : "Product search"));
  head.appendChild(el("div", "count", `${results.length} item${results.length === 1 ? "" : "s"}`));
  root.appendChild(head);

  if (results.length === 0) {
    root.appendChild(el("div", "empty", "No results."));
  } else {
    const grid = el("div", "grid");
    for (const product of results) grid.appendChild(renderCard(product));
    root.appendChild(grid);
  }

  const footer = el("div", "footer");
  const summary = el(
    "div",
    "added-summary",
    addedCodes.size > 0 ? `${addedCodes.size} item${addedCodes.size === 1 ? "" : "s"} added` : "",
  );
  const tellBtn = btn("Tell Claude →", tellClaude, "tell-claude");
  tellBtn.disabled = addedCodes.size === 0;
  footer.appendChild(summary);
  footer.appendChild(tellBtn);
  root.appendChild(footer);

  app?.sendSizeChanged?.({ height: document.documentElement.scrollHeight });
}

function renderCard(product) {
  const card = el("div", "card");
  card.dataset.code = product.code;

  const photoWrap = el("div", "photo-wrap");
  const imageUrl = (product.image_urls && product.image_urls[0]) || null;
  if (imageUrl) {
    const img = document.createElement("img");
    img.src = imageUrl;
    img.alt = product.name || "product photo";
    img.onerror = () => {
      photoWrap.innerHTML = "";
      photoWrap.appendChild(el("div", "photo-fallback", "No photo"));
    };
    photoWrap.appendChild(img);
  } else {
    photoWrap.appendChild(el("div", "photo-fallback", "No photo"));
  }
  card.appendChild(photoWrap);

  const body = el("div", "body");
  body.appendChild(el("div", "name", product.name || "Unnamed product"));
  if (product.package_size) body.appendChild(el("div", "package-size", product.package_size));

  const priceRow = el("div", "price-row");
  if (product.price != null) priceRow.appendChild(el("div", "price", `$${product.price.toFixed(2)}`));
  if (product.regular_price != null && product.regular_price !== product.price) {
    priceRow.appendChild(el("div", "regular-price", `$${product.regular_price.toFixed(2)}`));
  }
  body.appendChild(priceRow);

  const alreadyAdded = addedCodes.has(product.code);
  const addBtn = btn(alreadyAdded ? "✓ Added" : "Add", () => addToCart(product, card, addBtn), "add");
  if (alreadyAdded) {
    addBtn.disabled = true;
    addBtn.classList.add("added");
  }
  body.appendChild(addBtn);

  card.appendChild(body);
  return card;
}

// ---- actions ----
async function addToCart(product, cardEl, btnEl) {
  const existingError = cardEl.querySelector(".card-error");
  if (existingError) existingError.remove();

  if (!inHost) {
    console.info("[standalone] would add to cart:", product.code);
    return;
  }
  if (!can.serverTools()) {
    cardEl.appendChild(el("div", "card-error", "This client can't call server tools from a widget."));
    return;
  }

  btnEl.disabled = true;
  btnEl.textContent = "Adding…";
  try {
    const res = await app.callServerTool({
      name: "add_to_cart",
      arguments: { items: [{ product_code: product.code, quantity: 1 }] },
    });
    const data = extractToolData(res);
    if (res.isError || data?.error) {
      const message = data?.message || "Add to cart failed.";
      cardEl.appendChild(el("div", "card-error", message));
      btnEl.disabled = false;
      btnEl.textContent = "Add";
      log("warning", { event: "add_to_cart_failed", code: product.code, message });
      return;
    }
    addedCodes.add(product.code);
    btnEl.textContent = "✓ Added";
    btnEl.classList.add("added");
    render(); // refresh the footer's added-count/Tell Claude state
  } catch (e) {
    cardEl.appendChild(el("div", "card-error", String(e)));
    btnEl.disabled = false;
    btnEl.textContent = "Add";
    log("warning", { event: "add_to_cart_threw", code: product.code, error: String(e) });
  }
}

/**
 * The 2-way handoff, same canonical pattern as the reference this widget
 * was built against: updateModelContext stages a summary silently (no
 * model turn, last-write-wins), sendMessage is the only host->model call
 * that actually triggers one. Stage first, then send. On claude.ai web,
 * sendMessage shows a red "use caution" banner -- host safety UX, not
 * something this widget can suppress.
 */
async function tellClaude() {
  if (addedCodes.size === 0) return;
  const names = results.filter((p) => addedCodes.has(p.code)).map((p) => p.name || p.code);
  const summary = `Added from the product search widget: ${names.join(", ")}.`;
  if (!inHost) {
    console.info("[standalone] would tell Claude:", summary);
    return;
  }
  if (can.modelContext()) {
    await app.updateModelContext({ content: [{ type: "text", text: summary }] });
  }
  if (can.message()) {
    await app.sendMessage({ role: "user", content: [{ type: "text", text: summary }] });
  } else {
    log("warning", { event: "no_message_capability" });
  }
}

// ---- host context (theme/fonts) ----
function applyContext(ctx) {
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}

// ---- helpers ----
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text) node.textContent = text;
  return node;
}
function btn(label, onClick, cls) {
  const b = document.createElement("button");
  if (cls) b.className = cls;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

// ---- boot ----
async function boot() {
  if (!inHost) {
    render();
    return;
  }

  app = new App({ name: "PC Express Product Search", version: "1.0.0" });

  // Register handlers BEFORE connect() -- ontoolresult is one-shot and
  // fires immediately after the handshake.
  app.ontoolresult = async (res) => {
    const launch = extractToolData(res);
    if (!launch?.result_ref) {
      log("warning", { event: "missing_result_ref", data: launch });
      return;
    }
    query = launch.query || "";
    try {
      const dataRes = await app.callServerTool({
        name: "_interactive_search_results",
        arguments: { result_ref: launch.result_ref },
      });
      const data = extractToolData(dataRes);
      results = Array.isArray(data?.results) ? data.results : [];
    } catch (e) {
      log("warning", { event: "fetch_results_failed", error: String(e) });
      results = [];
    }
    render();
  };
  app.onhostcontextchanged = applyContext;

  await app.connect();
  const ctx = app.getHostContext();
  if (ctx) applyContext(ctx);

  log("info", { event: "connected", capabilities: caps() });
  render(); // render immediately; ontoolresult refines once data arrives
}

boot();
