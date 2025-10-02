#!/bin/bash

# Script to build and run the personal site Docker container
# This script will stop existing containers, build a new image, and run it

set -e  # Exit on any error

# Configuration
IMAGE_NAME="email-scraper"
CONTAINER_NAME="email-scraper-container"
PORT=5004

echo "🚀 Starting deployment of email scraper..."

# Function to check if container exists and is running
container_exists() {
    docker ps -a --format "table {{.Names}}" | grep -q "^${CONTAINER_NAME}$"
}

container_running() {
    docker ps --format "table {{.Names}}" | grep -q "^${CONTAINER_NAME}$"
}

# Stop and remove existing container if it exists
if container_exists; then
    echo "📦 Found existing container: ${CONTAINER_NAME}"
    
    if container_running; then
        echo "🛑 Stopping running container..."
        docker stop ${CONTAINER_NAME}
    fi
    
    echo "🗑️  Removing existing container..."
    docker rm ${CONTAINER_NAME}
else
    echo "📦 No existing container found"
fi

# Remove existing image if it exists
if docker images | grep -q "^${IMAGE_NAME}"; then
    echo "🗑️  Removing existing image: ${IMAGE_NAME}"
    docker rmi ${IMAGE_NAME}
else
    echo "🖼️  No existing image found"
fi

# Build the new Docker image
echo "🔨 Building new Docker image: ${IMAGE_NAME}"
docker build -t ${IMAGE_NAME} .

# Run the new container
echo "🚀 Starting new container: ${CONTAINER_NAME}"
docker run -d \
    --name ${CONTAINER_NAME} \
    -p ${PORT}:${PORT} \
    --restart unless-stopped \
    ${IMAGE_NAME}

# Check if container is running
sleep 2
if container_running; then
    echo "✅ Container is running successfully!"
    echo "🌐 Personal site is available at: http://localhost:${PORT}"
    echo "📊 Container status:"
    docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
else
    echo "❌ Container failed to start. Checking logs:"
    docker logs ${CONTAINER_NAME}
    exit 1
fi

echo "🎉 Deployment completed successfully!"
