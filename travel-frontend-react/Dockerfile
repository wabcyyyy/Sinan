# 构建前端静态资源后用 nginx 托管，并反代 /api 到后端。
# build 阶段是唯一的前端构建真相：本文件末尾的 caddy 阶段（生产边缘，见
# deploy/Caddyfile 与 docker-compose.yml 的 caddy 服务）复用同一产物，不重复构建步骤。
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install
COPY . .
RUN npm run build

# web = 本地/演示前端服务（compose frontend-react 显式 target；无 target 时会
# 错build到末尾的 caddy 段，5173 变 Caddy 默认页且 /api 反代消失）
FROM nginx:1.27-alpine AS web
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80

# 生产边缘镜像（docker-compose.yml 的 caddy 服务，target=caddy）：
# 只带静态产物，Caddyfile 由 compose 挂载——静态托管 + /api 反代 + 自动 TLS 一体。
FROM caddy:2-alpine AS caddy
COPY --from=build /app/dist /srv
