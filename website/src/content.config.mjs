// The pages: the repository's Markdown that src/site.mjs names, each with its path as its id.
import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { PAGES, ROOT } from "./site.mjs";

export const collections = {
  pages: defineCollection({
    loader: glob({ pattern: PAGES, base: ROOT, generateId: ({ entry }) => entry }),
  }),
};
