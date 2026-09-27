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
META_LOGIN_CONFIG_ID=                               # optional: Facebook Login for Business configuration id (used instead of permission names)
META_SCOPES=                                        # optional: comma-separated permission override
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
WHATSAPP_TEMPLATE_IMAGES=false                      # true once every template below has an approved IMAGE header
```

### WhatsApp templates carrying a flyer design

Facebook and Instagram posts already carry one of the five flyer designs. To have WhatsApp updates carry
one too, each of the three templates above must be edited in WhatsApp Manager to add an **IMAGE** header
component above the body text, and re-submitted for approval (Meta re-reviews template edits, usually
within a day). Once all three are approved with an image header, set `WHATSAPP_TEMPLATE_IMAGES=true` and
restart the API - staff then get a design picker (the same five styles as Facebook/Instagram) when sending
a broadcast, and the estate's flyer is sent as the header image. Leave this flag `false` (the default)
until the templates are updated - sending a header Meta does not expect will make the message fail.

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

**Use cases:** the app needs the *Manage everything on your Page* and *Manage messaging & content on Instagram* use cases (Facebook Login alone reports every Page/Instagram permission as "Invalid Scope"). If Meta will not combine them with Facebook Login, create the app with just those two.

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

## 6. Automatic posting plans

Marketing -> Social posts -> **Automatic posting plan** writes and schedules a run of different posts (for example one a day for a week, twice a day, or 2-3 a week). Migration `20260927_0036`.

- Angles rotate so neighbouring posts differ: plots available, sizes and prices, payment plan, featured plot, how to buy, progress update, area outlook, only-a-few-left (only when stock is genuinely low), plots already sold, site inspection, common question, buying-land checklist, talk to us. A template with no real data behind it is not used.
- Tones: friendly, professional (no emoji), urgent. Designs: bright promo, classic dark, or mixed.
- Captions are rewritten from live plot data just before each post goes out; if no plots are available nothing is posted (the post shows "Skipped").
- Posts more than 12 hours late (server down) are skipped rather than posted at a strange time. Pausing a plan holds its posts; resuming moves missed ones to the next free times.
- "Keep going automatically" adds the next batch when fewer than two days of posts remain.
- Editing a caption in Your posts keeps that post's own words (it is no longer rewritten).

### Scheduled & posted page and delivery records

- **Marketing -> Social posts -> "See and edit all posts"** (or straight after scheduling a plan) opens `/estates/<id>/marketing/posts`: every post grouped by day, with tabs Upcoming / Posted / Needs attention / Drafts / Cancelled / All and a plan filter. Open a post to edit the caption, time (Lagos), channels and design, try another wording, post now, retry, cancel, or give a skipped/cancelled post a new time.
- **Message delivery** (`/estates/<id>/notifications`) now also lists every Facebook / Instagram / WhatsApp Status post (sent, failed, skipped, with the reason and a link to the post) and every WhatsApp update, next to customer emails. Filter: Everything / Customer emails / Social posts / WhatsApp updates.
- **Default posting times (Lagos, WAT):** 1 a day 18:30; 2 a day 09:00 and 19:00; 3 a day 08:30, 13:00 and 19:30; weekly plans post at 18:30 on Wed (1/wk), Tue+Fri (2/wk), Mon+Wed+Fri (3/wk), Mon-Fri (5/wk). Custom times can be chosen per plan. The scheduler runs every minute, so posts go out within about a minute of their time.
