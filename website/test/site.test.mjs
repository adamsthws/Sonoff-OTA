// What src/site.mjs makes of the repository's paths and links.
import assert from "node:assert/strict";
import { test } from "node:test";
import { REPOSITORY, pages, rewrite, slug } from "../src/site.mjs";

const onSite = pages();
const github = `https://github.com/${REPOSITORY}`;

test("a page's address is its path, a README its directory's", () => {
  assert.equal(slug("README.md"), "");
  assert.equal(slug("docs/emulator.md"), "docs/emulator");
  assert.equal(slug("devices/sonoff-s61/README.md"), "devices/sonoff-s61");
});

test("every Markdown file is a page, wherever it is; nothing else is", () => {
  for (const path of ["README.md", "docs/what-happens.md", "devices/sonoff-s61/README.md", "tools/beken/README.md"]) {
    assert.ok(onSite.has(path), path);
  }
  assert.ok(!onSite.has("CLAUDE.md"));
  assert.ok([...onSite].every((path) => path.endsWith(".md") && !path.startsWith(".github/")));
});

test("a link to a page goes to its address, with its anchor", () => {
  assert.equal(rewrite("docs/emulator.md", "README.md", onSite), "/docs/emulator");
  assert.equal(rewrite("emulator.md#stopping", "docs/testing.md", onSite), "/docs/emulator#stopping");
  assert.equal(rewrite("../README.md", "docs/testing.md", onSite), "/");
  assert.equal(rewrite("devices/sonoff-s61/", "README.md", onSite), "/devices/sonoff-s61");
  assert.equal(rewrite("../tools/beken/README.md", "docs/x.md", onSite), "/tools/beken");
});

test("a link to anything else goes to it on GitHub", () => {
  assert.equal(rewrite("../tools/beken/backup.py", "docs/x.md", onSite), `${github}/blob/main/tools/beken/backup.py`);
  assert.equal(rewrite("tools/autoflash/", "README.md", onSite), `${github}/tree/main/tools/autoflash`);
  assert.equal(rewrite("LICENSE", "README.md", onSite), `${github}/blob/main/LICENSE`);
});

test("a picture stays beside the page; addresses and anchors are left alone", () => {
  assert.equal(rewrite("docs/FirmwareRoutes.svg", "README.md", onSite), "/docs/FirmwareRoutes.svg");
  for (const href of ["https://esphome.io/", "#lets-go", "mailto:x@example.com", "/liberate.sh"]) {
    assert.equal(rewrite(href, "README.md", onSite), href);
  }
});
