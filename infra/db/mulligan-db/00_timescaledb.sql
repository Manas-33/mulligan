-- Our init folder replaces the image's own, so install TimescaleDB here. Tables come from
-- gateway/migrations, which the gateway applies at startup.
CREATE EXTENSION IF NOT EXISTS timescaledb;
