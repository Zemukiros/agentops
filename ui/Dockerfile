# AgentOps UI: static build served by nginx (ADR-042). Build-time API origin for the client;
# the same origin is the only one the CSP lets the page talk to.
FROM node:24.19.0-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY . .
ARG VITE_API_URL=http://localhost:8000
ARG VITE_JAEGER_URL=
ENV VITE_API_URL=${VITE_API_URL} VITE_JAEGER_URL=${VITE_JAEGER_URL}
RUN npm run build

FROM nginx:1.30.6-alpine
# nginx's entrypoint renders /etc/nginx/templates/*.template with envsubst at start
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
ENV API_ORIGIN=http://localhost:8000 MAILPIT_ORIGIN=http://localhost:8025
EXPOSE 80
