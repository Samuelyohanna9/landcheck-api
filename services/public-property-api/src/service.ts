export type FetchLike = typeof fetch;

export type PublicProperty = {
  id: number;
  slug: string;
  name: string;
  tagline: string | null;
  description: string | null;
  location: string | null;
  logo_url: string | null;
  cover_url: string | null;
  contact_phone: string | null;
  whatsapp_number: string | null;
  show_prices: boolean;
  boundary: unknown;
  plots: Array<{
    id: number;
    plot_number: string;
    block: string | null;
    address: string | null;
    area_sqm: number | null;
    status: string;
    price: string | null;
    geometry: unknown;
    title_verified: boolean;
    approval_milestones: unknown[];
  }>;
  photos: Array<{
    id: string;
    title: string | null;
    media_url: string;
    captured_at: string | null;
  }>;
  counts: Record<string, number>;
};

export class PublicPropertyError extends Error {
  constructor(
    public readonly kind: "not_found" | "upstream_unavailable" | "invalid_upstream_payload",
    message: string,
  ) {
    super(message);
    this.name = "PublicPropertyError";
  }
}

const PROPERTY_SLUG = /^[a-z0-9][a-z0-9-]{0,139}$/;
const MAX_UPSTREAM_BYTES = 12 * 1024 * 1024;

export function isPublicPropertySlug(value: string): boolean {
  return PROPERTY_SLUG.test(value);
}

function upstreamUrl(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/+$/, "")}${path}`;
}

function publicAssetUrl(value: unknown, baseUrl: string): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  if (/^https?:\/\//i.test(value)) return value;
  if (!value.startsWith("/")) return null;
  return upstreamUrl(baseUrl, value);
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function objectRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}

async function readJson(response: Response): Promise<unknown> {
  const body = await response.text();
  if (new TextEncoder().encode(body).byteLength > MAX_UPSTREAM_BYTES) {
    throw new PublicPropertyError("invalid_upstream_payload", "The public property response is too large.");
  }
  try {
    return JSON.parse(body) as unknown;
  } catch {
    throw new PublicPropertyError("invalid_upstream_payload", "The public property response was invalid.");
  }
}

export function toPublicPropertyPayload(
  rawEstate: unknown,
  rawProgress: unknown,
  baseUrl: string,
): PublicProperty {
  const estate = objectRecord(rawEstate);
  const slug = stringOrNull(estate.slug);
  const id = typeof estate.id === "number" ? estate.id : null;
  const name = stringOrNull(estate.name);
  if (!slug || id === null || !name) {
    throw new PublicPropertyError("invalid_upstream_payload", "The public property payload is incomplete.");
  }

  const plots = Array.isArray(estate.plots) ? estate.plots : [];
  const progress = objectRecord(rawProgress);
  const updates = Array.isArray(progress.updates) ? progress.updates : [];

  return {
    id,
    slug,
    name,
    tagline: stringOrNull(estate.tagline),
    description: stringOrNull(estate.description),
    location: stringOrNull(estate.location),
    logo_url: publicAssetUrl(estate.logo_url, baseUrl),
    cover_url: publicAssetUrl(estate.cover_url, baseUrl),
    contact_phone: stringOrNull(estate.contact_phone),
    whatsapp_number: stringOrNull(estate.whatsapp_number),
    show_prices: estate.show_prices === true,
    boundary: estate.boundary ?? null,
    plots: plots.map((value) => {
      const plot = objectRecord(value);
      return {
        id: typeof plot.id === "number" ? plot.id : 0,
        plot_number: stringOrNull(plot.plot_number) || "",
        block: stringOrNull(plot.block),
        address: stringOrNull(plot.address),
        area_sqm: numberOrNull(plot.area_sqm),
        status: stringOrNull(plot.status) || "on_hold",
        price: stringOrNull(plot.price),
        geometry: plot.geometry ?? null,
        title_verified: plot.title_verified === true,
        approval_milestones: Array.isArray(plot.approval_milestones) ? plot.approval_milestones : [],
      };
    }),
    photos: updates.flatMap((value, index) => {
      const update = objectRecord(value);
      if (update.media_type !== "image") return [];
      const mediaUrl = publicAssetUrl(update.media_url, baseUrl);
      if (!mediaUrl) return [];
      return [{
        id: stringOrNull(update.id) || `photo-${index + 1}`,
        title: stringOrNull(update.title),
        media_url: mediaUrl,
        captured_at: stringOrNull(update.captured_at),
      }];
    }),
    counts: Object.fromEntries(
      Object.entries(objectRecord(estate.counts)).flatMap(([key, value]) =>
        typeof value === "number" && Number.isFinite(value) ? [[key, value]] : [],
      ),
    ),
  };
}

export async function fetchPublicProperty(
  slug: string,
  options: {
    upstreamBaseUrl: string;
    fetchImpl?: FetchLike;
    timeoutMs?: number;
  },
): Promise<PublicProperty> {
  if (!isPublicPropertySlug(slug)) {
    throw new PublicPropertyError("not_found", "Property not found.");
  }

  const fetchImpl = options.fetchImpl || fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 5000);
  const baseUrl = options.upstreamBaseUrl.replace(/\/+$/, "");
  const headers = { Accept: "application/json" };

  try {
    const estateResponse = await fetchImpl(
      upstreamUrl(baseUrl, `/estates/public/${encodeURIComponent(slug)}`),
      { method: "GET", headers, redirect: "error", signal: controller.signal },
    );
    if (estateResponse.status === 404) {
      throw new PublicPropertyError("not_found", "Property not found.");
    }
    if (!estateResponse.ok) {
      throw new PublicPropertyError("upstream_unavailable", "The property service is temporarily unavailable.");
    }
    const rawEstate = await readJson(estateResponse);

    let rawProgress: unknown = { updates: [] };
    try {
      const progressResponse = await fetchImpl(
        upstreamUrl(baseUrl, `/estates/public/${encodeURIComponent(slug)}/progress`),
        { method: "GET", headers, redirect: "error", signal: controller.signal },
      );
      if (progressResponse.ok) rawProgress = await readJson(progressResponse);
    } catch (error) {
      if (error instanceof PublicPropertyError && error.kind === "invalid_upstream_payload") throw error;
      // The listing remains useful when optional gallery data is temporarily unavailable.
    }

    return toPublicPropertyPayload(rawEstate, rawProgress, baseUrl);
  } catch (error) {
    if (error instanceof PublicPropertyError) throw error;
    throw new PublicPropertyError("upstream_unavailable", "The property service is temporarily unavailable.");
  } finally {
    clearTimeout(timer);
  }
}
