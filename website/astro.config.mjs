// The website's build: the repository's Markdown (src/site.mjs) rendered in
// the layout under src/layouts, written out as files for the Worker.
import { copyFileSync, globSync, mkdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, passthroughImageService } from "astro/config";
import { satteri } from "@astrojs/markdown-satteri";
import sitemap from "@astrojs/sitemap";
import { PICTURES, ROOT, pages, rewrite } from "./src/site.mjs";

const onSite = pages();

// Each link and picture in a page, rewritten for the site (src/site.mjs).
const links = ({ fileURL }) => {
  const from = relative(ROOT, fileURLToPath(fileURL));
  const visit = (node, ctx) => {
    const url = rewrite(node.url, from, onSite);
    if (url !== node.url) ctx.setProperty(node, "url", url);
  };
  return { name: "site-links", link: visit, definition: visit, image: visit };
};

// The pictures the pages show, beside them as in the repository.
const pictures = {
  name: "site-pictures",
  hooks: {
    "astro:build:done": ({ dir }) => {
      for (const path of globSync(PICTURES, { cwd: ROOT })) {
        const to = join(fileURLToPath(dir), path);
        mkdirSync(dirname(to), { recursive: true });
        copyFileSync(join(ROOT, path), to);
      }
    },
  },
};

export default defineConfig({
  site: "https://firmwareliberator.com",
  build: { format: "file" },
  image: { service: passthroughImageService() },
  markdown: { processor: satteri({ mdastPlugins: [links] }) },
  integrations: [sitemap(), pictures],
});
