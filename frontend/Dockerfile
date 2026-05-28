# Demo frontend image. Builds the Next.js app and serves it with `next start`.
# Behind Nginx on the same Lightsail box, so it only listens on the internal
# Docker network — never exposed directly.
#
# NOTE: NEXT_PUBLIC_API_BASE_URL is intentionally left UNSET so SSE/EventSource
# and all fetches use relative /api/* paths, which Nginx proxies to the backend
# (with buffering disabled for SSE). No CORS, no cross-origin EventSource.
FROM node:20-slim AS builder

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install
COPY . .
RUN npm run build

FROM node:20-slim AS runner
ENV NODE_ENV=production
WORKDIR /app

COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/package-lock.json* ./
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/next.config.js ./next.config.js

EXPOSE 3000
CMD ["npm", "run", "start"]
