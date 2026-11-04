// What the site serves, and where: the repository's Markdown pages, each at
// an address made from its path, and how a link between them is rewritten.
import { execFileSync } from "node:child_process";
import { globSync } from "node:fs";
import { dirname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

export const REPOSITORY = "adamsthws/firmware-liberator";
export const ROOT = fileURLToPath(new URL("../../", import.meta.url));

// Every Markdown file git tracks, so a link to one never leaves the site
// and nothing untracked is ever built in. Not CLAUDE.md, which only points
// at AGENTS.md, nor GitHub's own templates. Everything else - the tool, a
// config, LICENSE - is linked to on GitHub.
export const PAGES = execFileSync("git", ["ls-files", "-z", "--", "*.md"], { cwd: ROOT, encoding: "utf-8" })
  .split("\0")
  .filter((path) => path && path !== "CLAUDE.md" && !path.startsWith(".github/"));

// Pictures the pages show, copied beside them.
export const PICTURES = ["docs/*.svg", "docs/*.png"];

export const pages = () => new Set(PAGES);

// docs/emulator.md -> docs/emulator, devices/sonoff-s61/README.md ->
// devices/sonoff-s61, README.md -> "".
export function slug(path) {
  const page = path.replace(/\.md$/, "");
  return page === "README" ? "" : page.replace(/\/README$/, "");
}

// A link in the page at `from` (a repository path), as the site needs it:
// another page by its address, a picture as it is, anything else on GitHub.
export function rewrite(href, from, onSite) {
  if (/^([a-z][a-z0-9+.-]*:|[#/])/i.test(href)) return href;
  const [target, hash] = href.split(/(?=#)/);
  let path = normalize(join(dirname(from), decodeURIComponent(target))).replace(/\/$/, "");
  if (path === ".") path = "";
  if (path.startsWith("..")) return href;
  const readme = path ? `${path}/README.md` : "README.md";
  if (onSite.has(path)) return `/${slug(path)}${hash ?? ""}`;
  if (onSite.has(readme)) return `/${slug(readme)}${hash ?? ""}`;
  if (globSync(PICTURES, { cwd: ROOT }).includes(path)) return `/${path}`;
  const kind = /\.[^/]+$/.test(path) || path === "LICENSE" ? "blob" : "tree";
  return `https://github.com/${REPOSITORY}/${kind}/main/${path}${hash ?? ""}`;
}
