# Built from the repo root (compose: context ".", dockerfile dashboard/Dockerfile) so the shared
# contract types resolve at ../contracts/generated. VITE_* values are baked in at build time; the
# browser runs on the demo laptop, so it calls the gateway at 127.0.0.1:8000.
FROM node:24-slim
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY contracts/generated /app/contracts/generated
COPY dashboard ./
ARG VITE_MULLIGAN_API_URL=http://127.0.0.1:8000
ARG VITE_MULLIGAN_DASHBOARD_TOKEN
ARG SENTRY_DSN_DASHBOARD
ARG SENTRY_ENVIRONMENT=dev
ENV VITE_MULLIGAN_API_URL=$VITE_MULLIGAN_API_URL VITE_MULLIGAN_DASHBOARD_TOKEN=$VITE_MULLIGAN_DASHBOARD_TOKEN \
    SENTRY_DSN_DASHBOARD=$SENTRY_DSN_DASHBOARD SENTRY_ENVIRONMENT=$SENTRY_ENVIRONMENT
RUN npm run build
EXPOSE 5173
USER node
CMD ["npx", "vite", "preview", "--host", "0.0.0.0", "--port", "5173"]
