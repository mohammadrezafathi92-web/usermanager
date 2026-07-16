FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# --include=dev: vite/tailwind/postcss/autoprefixer are devDependencies but are
# required to run `vite build` below. Without this flag, npm ci silently skips
# devDependencies whenever NODE_ENV=production (or an equivalent npm config)
# is set in the build environment, leaving node_modules/.bin without `vite`
# and failing the build with "sh: vite: not found".
RUN npm ci --include=dev
COPY index.html vite.config.js tailwind.config.js postcss.config.js ./
COPY src ./src
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
