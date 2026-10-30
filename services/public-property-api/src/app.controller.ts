import {
  All,
  Controller,
  Get,
  MethodNotAllowedException,
  Param,
  Req,
} from "@nestjs/common";
import type { Request } from "express";
import { PublicPropertyService } from "./property.service";

@Controller()
export class AppController {
  constructor(private readonly properties: PublicPropertyService) {}

  @Get("healthz")
  health() {
    return { status: "ok", service: "public-property-api" };
  }

  @Get("health")
  healthAlias() {
    return { status: "ok", service: "public-property-api" };
  }

  @All("public/properties/:slug")
  async publicProperty(@Req() request: Request, @Param("slug") slug: string) {
    if (request.method !== "GET") {
      throw new MethodNotAllowedException();
    }
    return { property: await this.properties.findPublishedProperty(slug) };
  }
}
