"""Build docs/swagger.html: a standalone Swagger UI page with docs/openapi.json embedded.

Opens by double-click (no server needed). Swagger UI itself loads from cdnjs, so the browser
needs internet access. Regenerate after an API change:

    docker compose -f docker/docker-compose.yml run --rm -e APP_NAME="Assessment Platform API" \
        api python -m app.cli export-openapi
    python3 scripts/build_swagger_html.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWAGGER_UI_VERSION = "5.17.14"  # the integrity hashes in PAGE belong to this exact version

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__ - API reference</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__VER__/swagger-ui.min.css"
        integrity="sha384-+R8fx41ke61Yq4+j30C8Uh8fbNUEGy8Le9lzVmeMfiUNd+H3q8gN7qW9LM3rbesj"
        crossorigin="anonymous">
  <style>body { margin: 0; } .topbar { display: none; }</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script id="spec" type="application/json">__SPEC__</script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/__VER__/swagger-ui-bundle.min.js"
          integrity="sha384-5zikP7B6JemcHcJalIiFMnUirmSVBr3lZ85ClVPXO7mamWuqUixgyCMUPcKKfZLk"
          crossorigin="anonymous"></script>
  <script>
    window.ui = SwaggerUIBundle({
      spec: JSON.parse(document.getElementById("spec").textContent),
      dom_id: "#swagger-ui",
      deepLinking: true,
      docExpansion: "none",
      defaultModelsExpandDepth: 1,
      tagsSorter: "alpha",
      filter: true,
      tryItOutEnabled: false
    });
  </script>
</body>
</html>
"""


def main() -> None:
    spec = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    # "</" would end the script element early; the JSON escape keeps the data identical.
    embedded = json.dumps(spec, ensure_ascii=False).replace("</", "<\\/")
    html = (
        PAGE.replace("__TITLE__", spec["info"]["title"])
        .replace("__VER__", SWAGGER_UI_VERSION)
        .replace("__SPEC__", embedded)
    )
    target = ROOT / "docs" / "swagger.html"
    target.write_text(html, encoding="utf-8")
    operations = sum(
        1
        for item in spec["paths"].values()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    )
    print(f"wrote {target} ({operations} operations, {target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
