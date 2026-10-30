import assert from "node:assert/strict";
import { request } from "node:http";
import { test } from "node:test";

process.env.NODE_ENV = "test";
const { createPublicPropertyServer } = await import("../src/server.js");

function getJson(port: number, path: string, method = "GET"): Promise<{ status: number; body: Record<string, unknown> }> {
  return new Promise((resolve, reject) => {
    const client = request({ host: "127.0.0.1", port, path, method }, (response) => {
      const chunks: Buffer[] = [];
      response.on("data", (chunk: Buffer) => chunks.push(chunk));
      response.on("end", () => resolve({
        status: response.statusCode || 0,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>,
      }));
    });
    client.on("error", reject);
    client.end();
  });
}

test("serves the read-only public property route and rejects writes", async () => {
  const fetchImpl: typeof fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/progress")) return new Response(JSON.stringify({ updates: [] }), { status: 200 });
    return new Response(JSON.stringify({
      id: 12,
      slug: "amanah-estate",
      name: "Amanah Estate",
      show_prices: false,
      plots: [],
      counts: {},
    }), { status: 200 });
  };
  const server = createPublicPropertyServer("https://api.example.test", fetchImpl);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");

  try {
    const read = await getJson(address.port, "/public/properties/amanah-estate");
    assert.equal(read.status, 200);
    assert.equal((read.body.property as Record<string, unknown>).name, "Amanah Estate");

    const write = await getJson(address.port, "/public/properties/amanah-estate", "POST");
    assert.equal(write.status, 405);
  } finally {
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
