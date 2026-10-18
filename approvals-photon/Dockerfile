# Built from the repo root (compose: context ".", dockerfile approvals-photon/Dockerfile) so the
# shared contract types are available at ../contracts/generated like in development.
FROM node:24-slim
WORKDIR /app/approvals-photon
COPY approvals-photon/package.json approvals-photon/package-lock.json ./
RUN npm ci --omit=dev
COPY contracts/generated /app/contracts/generated
COPY approvals-photon/src ./src
ENV PHOTON_BIND_HOST=0.0.0.0 PORT=8100
EXPOSE 8100
USER node
CMD ["npx", "tsx", "src/main.ts"]
