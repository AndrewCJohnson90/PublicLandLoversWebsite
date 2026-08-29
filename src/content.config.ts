import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const articles = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/articles' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    tags: z.array(z.string()).default([]),
  }),
});

const maps = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/maps' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    // Public AGOL web map / web app embed URL (Embed API or app share URL)
    embedUrl: z.string().url(),
    region: z.string().optional(),
  }),
});

const threats = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/threats' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    // Same AGOL Experience Builder embed, tuned per-topic (bookmark/view
    // params). Andrew swaps this in per page after scaffolding.
    embedUrl: z.string().url(),
    order: z.number().default(0),
  }),
});

export const collections = { articles, maps, threats };
