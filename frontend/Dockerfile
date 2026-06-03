FROM node:20-alpine

WORKDIR /code

# Copy package descriptors first to cache layer dependencies cleanly
COPY package*.json ./

RUN npm install

# Copy over the rest of the frontend source code files
COPY . .

EXPOSE 5173

# Execute the development environment command
CMD ["npm", "run", "dev"]