import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { fetchPublicProperty, PublicPropertyError, type FetchLike } from "./service.js";

const PORT = Number.parseInt(process.env.PORT || "8090", 10);
const UPSTREAM_BASE_URL = process.env.LANDCHECK_PUBLIC_API_URL || "";
const ALLOWED_ORIGINS = new Set(
  (process.env.PUBLIC_PROPERTY_ALLOWED_ORIGINS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean),
);

function corsHeaders(request: IncomingMessage): Record<string, string> {
  const origin = request.headers.origin;
  if (!origin || !ALLOWED_ORIGINS.has(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Accept, Content-Type",
    Vary: "Origin",
  };
}

function sendJson(response: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(payload),
    ...headers,
  });
  response.end(payload);
}

export function createPublicPropertyServer(upstreamBaseUrl: string = UPSTREAM_BASE_URL, fetchImpl: FetchLike = fetch) {
  return createServer(async (request, response) => {
    const method = request.method || "GET";
    const requestUrl = new URL(request.url || "/", "http://localhost");
    const headers = corsHeaders(request);

    if (method === "OPTIONS") {
      if (request.headers.origin && !headers["Access-Control-Allow-Origin"]) {
        sendJson(response, 403, { detail: "Origin is not allowed." });
        return;
      }
      response.writeHead(204, headers);
      response.end();
      return;
    }

    if (requestUrl.pathname === "/healthz" || requestUrl.pathname === "/health") {
      if (method !== "GET") {
        sendJson(response, 405, { detail: "Method not allowed." }, { Allow: "GET" });
        return;
      }
      sendJson(response, 200, { status: "ok", service: "public-property-api" }, { "Cache-Control": "no-store", ...headers });
      return;
    }

    const propertyMatch = requestUrl.pathname.match(/^\/public\/properties\/([^/]+)\/?$/);
    if (!propertyMatch) {
      sendJson(response, 404, { detail: "Not found." }, headers);
      return;
    }
    if (method !== "GET") {
      sendJson(response, 405, { detail: "Method not allowed." }, { Allow: "GET", ...headers });
      return;
    }
    if (!upstreamBaseUrl) {
      sendJson(response, 503, { detail: "The property service is not configured." }, headers);
      return;
    }

    try {
      const property = await fetchPublicProperty(decodeURIComponent(propertyMatch[1]), { upstreamBaseUrl, fetchImpl });
      sendJson(response, 200, { property }, { "Cache-Control": "public, max-age=15, stale-while-revalidate=30", ...headers });
    } catch (error) {
      const propertyError = error instanceof PublicPropertyError ? error : null;
      const status = propertyError?.kind === "not_found" ? 404 : propertyError?.kind === "invalid_upstream_payload" ? 502 : 502;
      sendJson(response, status, { detail: propertyError?.kind === "not_found" ? "Property not found." : "The property service is temporarily unavailable." }, headers);
    }
  });
}

if (process.env.NODE_ENV !== "test") {
  if (!UPSTREAM_BASE_URL) {
    throw new Error("LANDCHECK_PUBLIC_API_URL must be set before starting public-property-api");
  }
  const server = createPublicPropertyServer();
  server.listen(PORT, "0.0.0.0", () => {
    console.log(`public-property-api listening on ${PORT}`);
  });
}
