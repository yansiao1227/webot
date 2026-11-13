FROM node:22-alpine
WORKDIR /app
COPY web/dist/server/index.js ./dist/server/index.js
COPY web/scripts/serve.mjs web/scripts/sqlite-storage.mjs ./scripts/
COPY web/migrations ./migrations/
COPY editions.sqlite ./editions.sqlite
COPY manifest.json Dockerfile release.sh ./
COPY verify-release.mjs ./scripts/verify-release.mjs
COPY verify-release.mjs ./verify-release.mjs
RUN printf '{"type":"module"}\n' > package.json
USER node
EXPOSE 8080
CMD ["node", "scripts/serve.mjs"]
