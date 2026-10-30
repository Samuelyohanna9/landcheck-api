import "reflect-metadata";
import { NestFactory } from "@nestjs/core";
import { AppModule } from "./app.module";

async function bootstrap(): Promise<void> {
  if (!process.env.LANDCHECK_PUBLIC_API_URL) {
    throw new Error("LANDCHECK_PUBLIC_API_URL must be set before starting public-property-api");
  }

  const app = await NestFactory.create(AppModule);
  const allowedOrigins = (process.env.PUBLIC_PROPERTY_ALLOWED_ORIGINS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);

  app.enableCors({
    origin: allowedOrigins.length > 0 ? allowedOrigins : false,
    methods: ["GET", "OPTIONS"],
    allowedHeaders: ["Accept", "Content-Type"],
  });

  const port = Number.parseInt(process.env.PORT || "8090", 10);
  await app.listen(port, "0.0.0.0");
  console.log(`public-property-api listening on ${port}`);
}

void bootstrap();
