import { createApp } from "./app.ts";
import { loadConfig } from "./config.ts";
import { httpGateway } from "./gateway.ts";
import { photonMessenger } from "./photon.ts";
import { replyLoop } from "./replies.ts";

const config = loadConfig();
const messenger = photonMessenger(config.photonProjectId, config.photonApiKey);
const server = createApp({ serviceToken: config.serviceToken, messenger });
const stop = new AbortController();

server.listen(config.port, config.host, () => {
  console.log(`approvals-photon listening on http://${config.host}:${config.port}`);
  console.log(`approver ${config.approverHandle}; replies go to ${config.gatewayUrl}`);
});

void replyLoop(messenger, httpGateway(config.gatewayUrl, config.serviceToken), { signal: stop.signal });

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    stop.abort();
    server.close(() => process.exit(0));
  });
}
