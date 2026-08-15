# Multi-stage build using only node:20-alpine (eliminates external nginx image mirror 403 errors)
FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . ./
ENV VITE_API_URL=/api
RUN npm run build

# Production runtime stage using node:20-alpine
FROM node:20-alpine

WORKDIR /app

COPY package.json ./
COPY server.js ./
COPY --from=builder /app/dist ./dist

ENV NODE_ENV=production \
    PORT=80 \
    BACKEND_HOST=backend \
    BACKEND_PORT=8000

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD node -e "require('http').get('http://127.0.0.1:80/health', (r) => process.exit(r.statusCode === 200 ? 0 : 1))"

CMD ["node", "server.js"]
