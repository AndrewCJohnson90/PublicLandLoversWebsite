import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const articles = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/articles' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    tags: z.array(z.string()).default([]),
    draft: z.boolean().default(false),
  }),
});

const maps = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/maps' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    embedUrl: z.string().url().optional(),
    region: z.string().optional(),
    draft: z.boolean().default(false),
  }),
});

const threats = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/threats' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    embedUrl: z.string().url().optional(),
    order: z.number().default(0),
    draft: z.boolean().default(false),
    // Optional — add this to a threat's frontmatter (e.g. pubDate: 2026-08-01)
    // once you want it to carry an article-style publish/update date for
    // search and AI citation purposes. Safe to leave off; nothing breaks.
    pubDate: z.coerce.date().optional(),
  }),
});

export const collections = { articles, maps, threats };
