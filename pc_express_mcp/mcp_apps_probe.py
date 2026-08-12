"""TEMPORARY: a minimal MCP Apps widget used only to empirically verify
whether Claude's mobile app renders MCP Apps UI at all, before investing
in a real interactive shopping widget.

A user found a runnable example proving MCP Apps works on Claude Desktop
(stdio), and Anthropic's own MCP Apps announcement lists "Claude: Web and
desktop" as supported clients -- mobile isn't listed, and a separate,
secondhand GitHub issue claims a mobile-specific widget-loading bug. Both
are worth confirming directly against this project's actual deployment
(HTTP/OAuth remote connector, which is what the mobile app uses) rather
than trusted secondhand. See docs/RESEARCH.md "Product photos in chat".

Delete this module (and its `apps` import/wiring in server.py) once the
mobile question is settled either way.
"""

from mcp.server.apps import Apps

apps = Apps()

_WIDGET_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 20px; background: #1e1e1e; color: #f0f0f0; }
  h2 { margin-top: 0; }
  button { padding: 12px 20px; border-radius: 10px; border: none; background: #34a853; color: white; font-size: 16px; font-weight: 600; }
  #status { margin-top: 16px; font-size: 15px; color: #9ad1f5; }
</style>
</head>
<body>
  <h2>MCP Apps test widget</h2>
  <p>If you can see this styled dark box with a green button below (not plain text), MCP Apps UI is rendering on this client.</p>
  <button id="btn">Tap me</button>
  <p id="status"></p>
  <script>
    document.getElementById('btn').addEventListener('click', function () {
      document.getElementById('status').textContent =
        'Button tapped at ' + new Date().toLocaleTimeString() + ' -- interactivity works too.';
    });
  </script>
</body>
</html>
"""

apps.add_html_resource(
    "ui://pc-express/mcp-apps-test.html",
    _WIDGET_HTML,
    name="mcp-apps-test",
    title="MCP Apps Test Widget",
)


@apps.tool(
    resource_uri="ui://pc-express/mcp-apps-test.html",
    description=(
        "Temporary diagnostic tool, not part of the real PC Express feature set. "
        "Renders a small interactive HTML widget (a button with live status text) "
        "via the MCP Apps extension. Its purpose is checking whether the connected "
        "client displays that widget or falls back to plain text."
    ),
)
def probe_mcp_apps_support() -> str:
    return (
        "Plain-text fallback content for this tool call. A client that renders MCP "
        "Apps widgets shows a styled box with a button instead of this sentence; a "
        "client that doesn't shows this sentence as-is."
    )
