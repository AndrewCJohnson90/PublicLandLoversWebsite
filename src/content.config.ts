import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const articles = defineCollection({
  loader: glob({ pattern: '**/*.{md,mdx}', base: './src/content/articles' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    tags: z.array(z.string()).default([]),
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
  }),
});

export const collections = { articles, maps, threats };
