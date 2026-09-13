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
# `npm run build` runs scripts/check-hooks.mjs first (see package.json), so
# the script has to be in the image. This COPY was the missing half of
# adding that check: the build passed locally, where the whole repo is on
# disk, and failed inside Docker with "Cannot find module" - which the
# self-update caught before restarting anything, exactly as designed.
COPY scripts ./scripts
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
