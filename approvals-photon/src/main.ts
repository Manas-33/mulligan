import { flush, report, sentryEnabled } from "./instrument.ts";
import { createApp } from "./app.ts";
import { loadConfig } from "./config.ts";
import { httpGateway } from "./gateway.ts";
import { photonMessenger } from "./photon.ts";
import { replyLoop } from "./replies.ts";
import { summaryLoop } from "./summaries.ts";

const config = loadConfig();
const messenger = photonMessenger(config.photonProjectId, config.photonApiKey);
const server = createApp({
  serviceToken: config.serviceToken,
  messenger,
  onError: (e, path) => report(e, "notify", { path }),
});
const stop = new AbortController();

server.listen(config.port, config.host, () => {
  console.log(`approvals-photon listening on http://${config.host}:${config.port}`);
  console.log(`approver ${config.approverHandle}; replies go to ${config.gatewayUrl}`);
  console.log(`sentry ${sentryEnabled ? "on" : "off (SENTRY_DSN_PHOTON not set)"}`);
});

const gateway = httpGateway(config.gatewayUrl, config.serviceToken);
void replyLoop(messenger, gateway, config.approverHandle, { signal: stop.signal, onError: (e) => report(e, "reply-loop") });
// Text the approver a summary whenever a rewind finishes.
void summaryLoop(gateway, messenger, config.approverHandle, {
  signal: stop.signal,
  onError: (e) => report(e, "summary-loop"),
});

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    stop.abort();
    server.close(() => void flush().then(() => process.exit(0)));
  });
}
