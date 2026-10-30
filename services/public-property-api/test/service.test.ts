import assert from "node:assert/strict";
import { test } from "node:test";
import { fetchPublicProperty, PublicPropertyError, toPublicPropertyPayload } from "../src/service";

const estatePayload = {
  id: 12,
  slug: "amanah-estate",
  name: "Amanah Estate",
  organization_email: "internal@example.test",
  tagline: "Verified land, clear next steps.",
  description: "A public property listing.",
  location: "Yola, Adamawa",
  logo_url: "/estates/public/amanah-estate/logo",
  cover_url: "/estates/public/amanah-estate/cover",
  contact_phone: "08000000000",
  whatsapp_number: "2348000000000",
  show_prices: true,
  boundary: { type: "Polygon", coordinates: [] },
  counts: { available: 1 },
  plots: [{
    id: 44,
    plot_number: "A-01",
    block: "A",
    address: "North corner",
    area_sqm: 500,
    status: "available",
    price: "25000000",
    geometry: { type: "Polygon", coordinates: [] },
    title_verified: true,
    approval_milestones: [],
  }],
};

test("normalizes only public property fields and resolves public assets", () => {
  const result = toPublicPropertyPayload(
    estatePayload,
    { updates: [{ id: "photo-1", media_type: "image", media_url: "/estates/public/amanah-estate/progress/photo-1/media", title: "Frontage" }] },
    "https://api.example.test",
  );

  assert.equal(result.name, "Amanah Estate");
  assert.equal(result.cover_url, "https://api.example.test/estates/public/amanah-estate/cover");
  assert.equal(result.photos[0]?.media_url, "https://api.example.test/estates/public/amanah-estate/progress/photo-1/media");
  assert.equal("organization_email" in result, false);
  assert.equal(result.plots[0]?.price, "25000000");
});

test("fetches the published estate and optional public photos without forwarding query parameters", async () => {
  const calls: string[] = [];
  const fetchImpl: typeof fetch = async (input) => {
    const url = String(input);
    calls.push(url);
    return new Response(
      url.endsWith("/progress") ? JSON.stringify({ updates: [] }) : JSON.stringify(estatePayload),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };

  const result = await fetchPublicProperty("amanah-estate", {
    upstreamBaseUrl: "https://api.example.test/",
    fetchImpl,
  });

  assert.equal(result.slug, "amanah-estate");
  assert.deepEqual(calls, [
    "https://api.example.test/estates/public/amanah-estate",
    "https://api.example.test/estates/public/amanah-estate/progress",
  ]);
});

test("rejects invalid slugs before making an upstream request", async () => {
  let called = false;
  await assert.rejects(
    fetchPublicProperty("../private", {
      upstreamBaseUrl: "https://api.example.test",
      fetchImpl: async () => {
        called = true;
        return new Response("{}", { status: 200 });
      },
    }),
    (error: unknown) => error instanceof PublicPropertyError && error.kind === "not_found",
  );
  assert.equal(called, false);
});

test("maps an upstream 404 to a public not-found error", async () => {
  await assert.rejects(
    fetchPublicProperty("missing-property", {
      upstreamBaseUrl: "https://api.example.test",
      fetchImpl: async () => new Response("Not found", { status: 404 }),
    }),
    (error: unknown) => error instanceof PublicPropertyError && error.kind === "not_found",
  );
});
