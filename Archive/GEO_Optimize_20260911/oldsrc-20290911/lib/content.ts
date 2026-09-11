// Drafts (draft: true in frontmatter) show up when running `npm run dev`
// locally, so you can preview them, but are completely excluded from
// `npm run build` — no page gets generated for them at all, so there's
// nothing to accidentally link to or stumble onto on the live site. Flip
// `draft: true` to `false` (or remove the line) when it's ready to go live.
export function notDraft<T extends { data: { draft?: boolean } }>(entry: T): boolean {
  return import.meta.env.DEV || !entry.data.draft;
}
