import { createApp } from "./app.ts";
import { loadConfig } from "./config.ts";

const config = loadConfig();
const server = createApp({ serviceToken: config.serviceToken });

server.listen(config.port, config.host, () => {
  console.log(`approvals-photon listening on http://${config.host}:${config.port}`);
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
