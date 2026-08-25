// Cloudflare Pages Function — handles POST /api/lead from work-with-us/index.astro
//
// Cloudflare Pages auto-detects anything under /functions as a serverless
// route, no extra config needed. This stub just logs the submission; wire
// up real email delivery before going live.
//
// Recommended free options:
//   - Resend (resend.com) — generous free tier, simple REST API
//   - Formspree — skip this file entirely and point the form's `action`
//     straight at your Formspree endpoint instead
//
// To use Resend: add a RESEND_API_KEY environment variable/secret in the
// Cloudflare Pages project settings, then uncomment the fetch() call below.

export async function onRequestPost(context) {
  const { request, env } = context;

  const formData = await request.formData();
  const payload = {
    name: formData.get('name'),
    org: formData.get('org'),
    email: formData.get('email'),
    project: formData.get('project'),
    message: formData.get('message'),
  };

  // Basic validation
  if (!payload.name || !payload.email) {
    return new Response('Missing required fields', { status: 400 });
  }

  // --- Uncomment once RESEND_API_KEY is set in Cloudflare Pages env vars ---
  // await fetch('https://api.resend.com/emails', {
  //   method: 'POST',
  //   headers: {
  //     Authorization: `Bearer ${env.RESEND_API_KEY}`,
  //     'Content-Type': 'application/json',
  //   },
  //   body: JSON.stringify({
  //     from: 'leads@publiclandlovers.com',
  //     to: 'hello@publiclandlovers.com',
  //     subject: `New lead: ${payload.name}`,
  //     text: JSON.stringify(payload, null, 2),
  //   }),
  // });

  console.log('New lead submission:', payload);

  return Response.redirect(new URL('/work-with-us/?sent=1', request.url), 303);
}
