import { BadGatewayException, Injectable, NotFoundException, ServiceUnavailableException } from "@nestjs/common";
import {
  fetchPublicProperty,
  PublicPropertyError,
  type FetchLike,
  type PublicProperty,
} from "./service";

@Injectable()
export class PublicPropertyService {
  constructor(
    private readonly upstreamBaseUrl: string = process.env.LANDCHECK_PUBLIC_API_URL || "",
    private readonly fetchImpl: FetchLike = fetch,
  ) {}

  async findPublishedProperty(slug: string): Promise<PublicProperty> {
    if (!this.upstreamBaseUrl) {
      throw new ServiceUnavailableException("The property service is not configured.");
    }
    try {
      return await fetchPublicProperty(slug, {
        upstreamBaseUrl: this.upstreamBaseUrl,
        fetchImpl: this.fetchImpl,
      });
    } catch (error) {
      if (error instanceof PublicPropertyError && error.kind === "not_found") {
        throw new NotFoundException("Property not found.");
      }
      if (error instanceof PublicPropertyError && error.kind === "invalid_upstream_payload") {
        throw new BadGatewayException("The public property data was invalid.");
      }
      throw new BadGatewayException("The property service is temporarily unavailable.");
    }
  }
}
