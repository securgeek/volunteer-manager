# Build stages only; none of node ships. The 24 line rather than an exact patch, which Docker
# Hub stops rebuilding (see backend/Dockerfile).
FROM node:24-alpine AS base

FROM base AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM base AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY public/ ./public
COPY src/ ./src
COPY index.html vite.config.js package.json ./
RUN --mount=type=cache,target=/root/.npm npm run build

FROM nginx:alpine
# Alpine fixes newer than the nginx image itself.
RUN apk upgrade --no-cache
# No TLS here on purpose. The only thing in front of this container is the tunnel client on
# the host, with HTTPS terminated at the edge, dialing the loopback-only host publishes:
# 8080 serves the full site, 8082 the volunteer portal alone (default.conf). Both above
# 1024 so nothing in the stack needs a privileged bind, which matters under rootless.
# default.conf is a template: the image's entrypoint writes it to conf.d at start, filling
# ${NGINX_LOCAL_RESOLVERS} from the container's /etc/resolv.conf so the portal upstream
# resolves under both Docker and podman. The filter keeps envsubst away from every other
# variable, nginx's own $remote_addr and friends included.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER=^NGINX_LOCAL_RESOLVERS\$
COPY default.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

RUN addgroup -g 1001 -S appuser && \
    adduser  -u 1001 -S appuser -G appuser && \
    touch /run/nginx.pid && \
    mkdir -p /cache/nginx && \
    chown -R appuser:appuser \
        /run/nginx.pid /cache /usr/share/nginx/html \
        /var/cache/nginx /var/log/nginx /etc/nginx/conf.d

USER appuser
EXPOSE 8080 8082
# The nginx image stops with SIGQUIT, a graceful shutdown that waits for open connections
# to close. Proxied /api/events SSE streams never close, so every stop ran to the 10s
# SIGKILL. SIGTERM is nginx's fast shutdown; browsers' EventSource reconnects by itself.
STOPSIGNAL SIGTERM
CMD ["nginx", "-g", "daemon off;"]
