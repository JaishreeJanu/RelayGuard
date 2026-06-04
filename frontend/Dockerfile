# FROM node:20-alpine

# WORKDIR /code

# # Copy package descriptors first to cache layer dependencies cleanly
# COPY package*.json ./

# RUN npm install

# # Copy over the rest of the frontend source code files
# COPY . .

# EXPOSE 5173

# # Execute the development environment command
# CMD ["npm", "run", "dev"]


# --- STAGE 1: COMPILE THE REACT ASSETS ---
FROM node:20-alpine AS build
WORKDIR /app

ARG VITE_API_URL
ARG VITE_VENDOR_URL
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_VENDOR_URL=$VITE_VENDOR_URL

COPY package*.json ./
RUN npm install
COPY . .
# Compile React source files into an optimized /dist directory
RUN npm run build

# --- STAGE 2: SERVE ASSETS SECURELY VIA NGINX ---
FROM nginx:alpine
# Copy compiled static files from Stage 1 straight to Nginx's HTML root
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]