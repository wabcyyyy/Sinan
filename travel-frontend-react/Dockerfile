# 构建前端静态资源后用 nginx 托管，并反代 /api 到后端。
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

# web = 本地/演示前端服务。
FROM nginx:1.27-alpine AS web
# 镜像溯源（T8a）：与 travel-agent-python/Dockerfile 同一组 build-arg，
# 值来自 deploy/docker-compose.prod.yml 的 build.args（源头是 deploy/deploy.sh）。
ARG SINAN_GIT_REVISION=unknown
ARG SINAN_SOURCE_URL=unknown
ARG SINAN_BUILD_CREATED_AT=unknown
LABEL org.opencontainers.image.title="sinan-frontend-react" \
    org.opencontainers.image.revision="${SINAN_GIT_REVISION}" \
    org.opencontainers.image.version="${SINAN_GIT_REVISION}" \
    org.opencontainers.image.source="${SINAN_SOURCE_URL}" \
    org.opencontainers.image.created="${SINAN_BUILD_CREATED_AT}"
COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=project LICENSE THIRD_PARTY_NOTICES.md /usr/share/nginx/html/licenses/
COPY --from=build /app/src/assets/img/CREDITS.md /usr/share/nginx/html/licenses/photo-credits.md
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
