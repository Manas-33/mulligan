// Runtime settings, read once from the environment (docker compose passes .env through).

export type Config = {
  port: number;
  host: string;
  serviceToken: string;
  gatewayUrl: string;
  photonProjectId: string;
  photonApiKey: string;
  approverHandle: string;
};

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const required = (name: string): string => {
    const value = env[name]?.trim();
    if (!value) throw new Error(`${name} is not set`);
    return value;
  };
  return {
    port: Number(env.PORT ?? 8100),
    // Inside the container the service must listen on every interface; the host port stays 127.0.0.1 only.
    host: env.PHOTON_BIND_HOST ?? "127.0.0.1",
    serviceToken: required("MULLIGAN_SERVICE_TOKEN"),
    gatewayUrl: env.MULLIGAN_API_URL ?? "http://127.0.0.1:8000",
    photonProjectId: required("PHOTON_PROJECT_ID"),
    photonApiKey: required("PHOTON_PROJECT_SECRET"),
    // APPROVER_HANDLE (iMessage phone or Apple ID email) wins over the older APPROVER_PHONE.
    approverHandle: env.APPROVER_HANDLE?.trim() || required("APPROVER_PHONE"),
  };
}
