# Estate social posting - setup and Meta review guide

What it does (Marketing -> **Social posts** tab):

| Feature | Needs Meta approval? |
|---|---|
| Caption templates, copy caption, download image, **Share to Status** button, scheduled reminders | No - works today |
| Automatic **Facebook Page** and **Instagram** (post + story) publishing, scheduling | Yes - app review |
| **WhatsApp updates** to buyers who opted in (templates) | Yes - WhatsApp Business setup + template approval |

## 1. Server configuration

Set these in the API environment (`.env` next to `docker-compose.yml`) and add them to the `api` service `environment:` block if you pass variables explicitly:

```
SOCIAL_SECRET_KEY=<random string, 32+ characters>   # encrypts stored tokens, signs links. Keep it secret. Do not change it later.
META_APP_ID=<from developers.facebook.com>
META_APP_SECRET=<from developers.facebook.com>
META_GRAPH_VERSION=v23.0                            # optional, default v23.0
LANDCHECK_API_PUBLIC_URL=https://api.landcheck.online   # must be public HTTPS - Meta fetches post images from it
LANDCHECK_WEB_URL=https://landcheck.online

# WhatsApp Business (Cloud API) - only for opt-in updates
WHATSAPP_CLOUD_TOKEN=<permanent system-user token>
WHATSAPP_PHONE_NUMBER_ID=<sending number id>
WHATSAPP_VERIFY_TOKEN=<any string you choose>
WHATSAPP_APP_SECRET=<app secret; falls back to META_APP_SECRET>
# optional template-name overrides (defaults shown)
WA_TEMPLATE_NEW_PLOTS=estate_new_plots
WA_TEMPLATE_PRICE_UPDATE=estate_price_update
WA_TEMPLATE_INSPECTION=estate_inspection_invite
```

Then `docker compose build api && docker compose up -d api` (runs `alembic upgrade head`, migration `20260926_0035`).
`cryptography` is now in `requirements.txt`, so a rebuild is required.

Until `META_APP_ID`, `META_APP_SECRET` and `SOCIAL_SECRET_KEY` are all set, the Facebook/Instagram connect button stays disabled ("Coming soon"). The WhatsApp box appears on public Estate pages only when the two `WHATSAPP_*` values are set.

## 2. Meta developer app

1. developers.facebook.com -> Create app -> type **Business**. Add the products **Facebook Login for Business** (or Facebook Login), **Instagram** (Instagram API with Facebook Login) and, for messaging, **WhatsApp**.
2. Settings -> Basic:
   - **Privacy Policy URL:** `https://landcheck.online/privacy`
   - **User data deletion:** choose *Data deletion request URL* -> `https://api.landcheck.online/estates/marketing/social/meta/data-deletion`
   - (or instructions URL: `https://landcheck.online/data-deletion`)
   - App domains: `landcheck.online`, `api.landcheck.online`; Category: Business and pages.
3. Facebook Login -> Settings:
   - **Valid OAuth Redirect URIs:** `https://api.landcheck.online/estates/marketing/social/meta/callback`
   - **Deauthorize callback URL:** `https://api.landcheck.online/estates/marketing/social/meta/deauthorize`
4. Complete **Business verification** in Meta Business Settings (needed for Advanced Access to page permissions). Have your CAC documents ready.
5. While in Development mode only people with a role on the app can connect - add yourself and a test Page/Instagram account as Tester/Developer and test the whole flow first.

## 3. App review - permissions to request

| Permission | Why (text for the submission) |
|---|---|
| `pages_show_list` | Lets the estate company choose which of its Facebook Pages LandCheck may post to. |
| `pages_manage_posts` | Publishes the marketing posts (image + caption) that the company itself creates in LandCheck to its own Page. |
| `pages_read_engagement` | Required by Meta to read the Page's basic details (name, linked Instagram account) needed to publish. |
| `instagram_basic` | Reads the Instagram Business account linked to the chosen Page so we know where to publish. |
| `instagram_content_publish` | Publishes the company's own posts and stories (image + caption) to its Instagram Business account. |
| `business_management` | Only if the company's Pages sit inside a Business portfolio (Meta returns no Pages otherwise). Drop it from `SCOPES` in `social_meta.py` if you do not need it. |

For each permission Meta asks for a **screencast**. Record one continuous 3-4 minute video (English narration or captions):
1. Sign in to LandCheck Estates -> Marketing -> Social posts.
2. Click **Connect** -> Facebook login and permission dialog -> choose a Page -> back in LandCheck the Page and Instagram account appear as Connected.
3. Pick a template, edit the caption, choose Facebook + Instagram, click **Post now** -> show the post live on the Facebook Page and on Instagram.
4. Schedule a post 5 minutes ahead; show it publish on time.
5. Click **Disconnect** and show the account removed.
Provide a test login (a reviewer account with the Estate created and published) in the submission notes, plus a test Facebook user that has a Page and an Instagram Business account. Meta also asks for the data-handling answers - the privacy policy section "Social media accounts and WhatsApp updates" explains them.

## 4. WhatsApp updates

1. WhatsApp Manager -> create a message template for each preset (category **Marketing**, language **English**). Names must match the env values above. Suggested bodies (variables in this order: first name, estate name, detail, link):
   - `estate_new_plots`: "Hello {{1}}, new plots are now available at {{2}}, from {{3}}. View the live map and prices: {{4}}. Reply STOP to stop updates."
   - `estate_price_update`: "Hello {{1}}, there is an update at {{2}}: {{3}}. Details: {{4}}. Reply STOP to stop updates."
   - `estate_inspection_invite`: "Hello {{1}}, you are invited to a site inspection at {{2}}: {{3}}. Book your place: {{4}}. Reply STOP to stop updates."
2. Webhook: Callback URL `https://api.landcheck.online/estates/marketing/social/whatsapp/webhook`, Verify token = `WHATSAPP_VERIFY_TOKEN`; subscribe to **messages**. STOP replies then opt people out automatically.
3. Messages go out in small batches every minute (about 80/min) to people who ticked the opt-in box. Meta charges per marketing conversation - check current rates.

## 5. How it behaves

- **Token safety:** page tokens are stored encrypted (Fernet, key derived from `SOCIAL_SECRET_KEY`) and are never returned by the API.
- **Images:** Meta downloads post images from a signed link that expires after 24 hours. QR codes are left off social images.
- **Instagram captions:** links are removed and replaced with "Link in bio" (Instagram does not make caption links clickable). Facebook keeps the link.
- **Retries:** "Retry" only resends channels that failed; a channel that already posted is never posted twice.
- **Expired/revoked token** (Meta error 190): the account switches to "Reconnect needed" and the post shows the reason.
- **Manual channels** (WhatsApp Status, Other): the scheduled time emails the team (owners, managers, marketers) with a link that opens the post; after posting, click **Mark as posted**.
- **Permissions:** owners and managers (and the new `marketing.manage` permission, given to the marketer role) can create, schedule, connect and broadcast; everyone with estate access can view.
- **Data deletion:** Meta's deauthorize/data-deletion callbacks delete the connected accounts for that Facebook user.
