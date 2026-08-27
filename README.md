# Public Land Lovers — Website

Static site built with [Astro](https://astro.build), deployable for free on
[Cloudflare Pages](https://pages.cloudflare.com). See the companion
`PublicLandLovers-Website-Work-Plan.md` doc for the full strategy this
scaffold implements.

## Run it locally

```bash
npm install
npm run dev        # http://localhost:4321
```

## Build

```bash
npm run build       # outputs to /dist

npm run preview     # preview the production build locally
```

## Deploy — Cloudflare Pages (recommended, free)

1. Push this repo to GitHub.
2. In the Cloudflare dashboard: **Workers & Pages → Create → Pages → Connect
   to Git**, pick this repo.
3. Build settings:
   - Framework preset: **Astro**
   - Build command: `npm run build`
   - Output directory: `dist`
4. Deploy. Cloudflare Pages auto-detects the `functions/` folder and wires
   up `/api/lead` as a serverless function — no extra config needed.
5. **Custom domain:** Pages project → Custom domains → add
   `publiclandlovers.com`. If your domain's nameservers point at
   Cloudflare, this is a couple of clicks; otherwise Cloudflare gives you a
   CNAME to add at Porkbun.

## Content

- **Articles:** add a `.md` file to `src/content/articles/`. Frontmatter:
  `title`, `description`, `pubDate`, `tags`.
- **Maps:** add a `.md` file to `src/content/maps/`. Frontmatter: `title`,
  `description`, `embedUrl` (your AGOL Instant App / Experience Builder
  share URL), optional `region`. The page just iframes `embedUrl` — nothing
  else to configure.

Pages are generated automatically from these folders; no need to touch
routing code to publish a new article or map.

## Lead form → email

`functions/api/lead.js` is a Cloudflare Pages Function stub that receives
the `/work-with-us/` form POST. It currently just logs the submission.
Two ways to finish wiring it:

- **Resend** (recommended, free tier): uncomment the `fetch()` block in
  `lead.js`, add a `RESEND_API_KEY` secret in the Cloudflare Pages project
  settings.
- **Formspree** (zero backend code): delete `functions/api/lead.js` and
  point the form's `action` in `src/pages/work-with-us/index.astro` at your
  Formspree endpoint instead.

## Design system

Tokens live in `src/styles/global.css`:

- **Palette:** topo-map paper (`--paper`), sagebrush (`--sage`), canyon
  rust (`--rust`), desert dusk (`--dusk`), trail-marker ochre (`--ochre`).
- **Type:** Fraunces (display), Inter (body), JetBrains Mono (labels,
  coordinates, utility text).
- **Signature element:** `src/components/Contour.astro` — nested
  topographic contour lines used as hero backdrop and section dividers,
  a direct callback to the GIS work the site showcases. Swap the path
  data if you want a different contour shape; it's plain SVG.

## Not yet wired up (by design, see work plan doc)

- Email list signup form on the homepage is a static `<form>` — connect it
  to MailerLite/Buttondown's embed snippet or API when ready.
- Social embeds (latest IG/TikTok posts) — add via their oEmbed APIs when
  ready; kept out of this scaffold to avoid extra JS weight up front.
- Shop — add a `/shop/` route with Stripe Payment Links or Gumroad embeds
  once you have a first product.
