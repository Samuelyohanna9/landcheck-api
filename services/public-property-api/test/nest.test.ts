import assert from "node:assert/strict";
import { test } from "node:test";
import { AppController } from "../src/app.controller";
import { PublicPropertyService } from "../src/property.service";

test("Nest controller exposes health and read-only property behavior", async () => {
  const property = { id: 12, slug: "amanah-estate", name: "Amanah Estate" } as never;
  const service = { findPublishedProperty: async () => property } as unknown as PublicPropertyService;
  const controller = new AppController(service);

  assert.deepEqual(controller.health(), { status: "ok", service: "public-property-api" });
  assert.deepEqual(
    await controller.publicProperty({ method: "GET" } as never, "amanah-estate"),
    { property },
  );
  await assert.rejects(
    controller.publicProperty({ method: "POST" } as never, "amanah-estate"),
    /Method Not Allowed/,
  );
});
