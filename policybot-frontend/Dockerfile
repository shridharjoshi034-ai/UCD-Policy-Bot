FROM node:20-alpine

# Set working directory
WORKDIR /app

# Copy package files
COPY package.json package-lock.json ./

# Install dependencies
RUN npm ci

# Copy application code
COPY . .

# Expose port
EXPOSE 5173

# Start the Vite development server with --host to expose it outside the container
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
