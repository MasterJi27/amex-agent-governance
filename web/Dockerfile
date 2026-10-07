FROM node:24-alpine AS build
WORKDIR /web
COPY package.json ./
RUN npm install
COPY . .
ARG VITE_API_URL=http://localhost:8001
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
