# The website

[firmwareliberator.com](https://firmwareliberator.com) is the repository's
README and docs as web pages, built with [Astro](https://astro.build) and
served by a Cloudflare Worker that holds nothing but the built files. The
pages are written once, as the repository's Markdown; this folder is only
how they become a site.

## What is here

| | |
|---|---|
| `src/site.mjs` | Which Markdown files are pages (`PAGES`: every one git tracks), the address of each, and how a link between them is rewritten |
| `src/content.config.mjs` | Those files as Astro's `pages` collection, read from the repository root |
| `src/pages/[...slug].astro` | One page per file, titled by its first heading |
| `src/pages/404.astro` | What an address with no page answers |
| `src/layouts/Page.astro` | Every page's frame: GitHub's Markdown styling, dark unless the reader picks light, the GitHub button, Cloudflare Web Analytics (no cookies) |
| `public/` | Copied as they are: the icons and stylesheets, `robots.txt`, `_redirects` |
| `astro.config.mjs` | The build: Markdown through Astro's Sätteri with the link rewriting, pictures copied beside their pages, a sitemap |
| `test/` | The link rewriting's tests (`npm test`, Node's own runner) |
| `wrangler.jsonc` | The Worker: the files in `dist/`, the 404 page, and both names pointed at it |

## Addresses

Every Markdown file git tracks is a page, but `CLAUDE.md` and `.github/`'s
templates. A page's address is its path without `.md`: `docs/emulator.md` is
`/docs/emulator`, a folder's `README.md` is the folder (`/devices/sonoff-s61`),
and the README is `/`. A link to another page goes to its address; a link to
anything else in the repository - the tool, a config, `LICENSE` - goes to it
on GitHub; pictures under `docs/` are served beside the pages. The Worker
also answers `/docs/emulator.html` (by redirect), as the old GitHub Pages
site's addresses were.

`public/_redirects` sends `/liberate.sh` and `/liberate.ps1` to the latest
release's, with a 302 so no browser keeps it past the next release.
`tests/repo/test_site.py` checks each is a file every release carries.

## Building it

Node 22 or newer:

    cd website
    npm ci --ignore-scripts
    npm test
    npm run build          # into dist/
    npx astro preview      # dist/ at http://localhost:4321

Packages come only from `package-lock.json`, by hash, with no install
scripts run. Dependabot moves them, a week after each release
(`.github/dependabot.yml`), and the dependabot-merge workflow merges its pull
request once it has been open a week with its checks passed.

## Deploying it

`.github/workflows/website.yml` tests and builds it for each pull request,
and on each push to `main` deploys it with wrangler from the `website`
environment, which only `main` can use (`tools/repo/github_settings.py`).
Its secret `CLOUDFLARE_API_TOKEN` is one Cloudflare API token:

- Individual Workers - `firmware-liberator`: Editor (the Worker, and only
  it: the account's other Workers are out of its reach)
- Zone `firmwareliberator.com` - Workers Routes: Write, DNS: Write (its
  custom domains), Zone: Read, Zone Settings: Write, Dynamic URL Redirects:
  Write (the settings below)

A Worker can only be picked once it exists, so the very first one was
uploaded by hand with an account-wide token, with no domain.

Set it with `gh secret set CLOUDFLARE_API_TOKEN --env website -R adamsthws/firmware-liberator`.

## The zone's settings

What the zone is set to beyond the Worker - `www.` sent on to the bare
domain, HTTPS only, TLS 1.2 or newer - is written down, with why, in
`tools/repo/cloudflare_settings.py`. The cloudflare-settings workflow sets
it on each push to `main` that changes it, and checks it weekly, failing if
it was changed in the dashboard. Locally, with the token in
`CLOUDFLARE_API_TOKEN` (the environment or `.env`),
`python3 tools/repo/cloudflare_settings.py` says what differs, and `... apply`
sets it.
