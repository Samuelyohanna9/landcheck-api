import { Module } from "@nestjs/common";
import { AppController } from "./app.controller";
import { PublicPropertyService } from "./property.service";

@Module({
  controllers: [AppController],
  providers: [PublicPropertyService],
})
export class AppModule {}
