/**
 * Load one scene into index.html so it can be checked and previewed in Studio.
 *
 *   node tools/use-scene.mjs scene02
 *
 * Scene files live in compositions/. The project may only have ONE root-level
 * composition (hyperframes errors with `multiple_root_compositions` otherwise), so
 * index.html is the single entry point and scenes are swapped into it.
 *
 * Asset paths stay project-root-relative in every file — `assets/…`, `.media/…`, never
 * `../`. The renderer resolves them against the project root regardless of where the
 * composition file lives, and `../` trips `invalid_parent_traversal_in_asset_path`.
 * So the copy is verbatim.
 */
import fs from "node:fs";
import path from "node:path";

const name = process.argv[2];
if (!name) {
  const available = fs
    .readdirSync("compositions")
    .filter((f) => f.endsWith(".html"))
    .map((f) => "  " + path.basename(f, ".html"))
    .join("\n");
  console.error(`usage: node tools/use-scene.mjs <scene>\n\navailable:\n${available}`);
  process.exit(1);
}

const src = path.join("compositions", `${name}.html`);
if (!fs.existsSync(src)) {
  console.error(`no such scene: ${src}`);
  process.exit(1);
}

fs.writeFileSync("index.html", fs.readFileSync(src, "utf8"));
console.log(`index.html <- ${src}`);
