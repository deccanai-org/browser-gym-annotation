# Build the SPA, then serve it statically via nginx (which also proxies /api).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Where the live pane reaches the live-browser service. Empty (default) keeps the
# built-in localhost:8877 for local; a hosted build passes the live-browser's
# public URL: docker build --build-arg VITE_LIVE_BASE=https://live-browser-...
ARG VITE_LIVE_BASE=""
ENV VITE_LIVE_BASE=$VITE_LIVE_BASE
RUN npm run build

FROM nginx:alpine
# nginx's entrypoint renders the template with envsubst using these env vars.
# Only substitute our vars so nginx runtime vars ($host, $uri, …) are untouched.
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
ENV PORT=8080 BACKEND_ORIGIN=http://backend:8090 BACKEND_HOST=backend \
    NGINX_ENVSUBST_FILTER="PORT|BACKEND_ORIGIN|BACKEND_HOST"
EXPOSE 8080
