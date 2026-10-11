# 构建前端静态资源后用 nginx 托管，并反代 /api 到后端。
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

# web = 本地/演示前端服务。
FROM nginx:1.27-alpine AS web
COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=project LICENSE THIRD_PARTY_NOTICES.md /usr/share/nginx/html/licenses/
COPY --from=build /app/src/assets/img/CREDITS.md /usr/share/nginx/html/licenses/photo-credits.md
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
