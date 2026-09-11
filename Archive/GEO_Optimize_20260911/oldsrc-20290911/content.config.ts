import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const articles = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/articles' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    tags: z.array(z.string()).default([]),
    // Set draft: true to stage content locally without it appearing (or
    // even being built) on the live production site. See src/lib/content.ts.
    draft: z.boolean().default(false),
  }),
});

const maps = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/maps' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    // Optional now — the map itself is placed inline in the body via the
    // <MapEmbed /> component (see src/components/MapEmbed.astro), so you
    // can position it wherever you want relative to your write-up. This
    // field is kept around for reference/backward compatibility only and
    // isn't rendered automatically by the page template anymore.
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
    // Same note as above — placed inline via <MapEmbed /> in the body now.
    embedUrl: z.string().url().optional(),
    order: z.number().default(0),
    draft: z.boolean().default(false),
  }),
});

export const collections = { articles, maps, threats };
