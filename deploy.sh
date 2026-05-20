#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "Starting deployment setup..."

# 1. Update and install system dependencies
echo "Installing system dependencies..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git wget curl \
  libnss3 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
  libxcomposite1 libxrandr2 libgbm1 libgtk-3-0 libasound2

# 2. Setup Virtual Environment
echo "Setting up Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# 3. Install Python requirements
echo "Installing Python packages..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Install Playwright browsers
echo "Installing Playwright browsers..."
python -m playwright install --with-deps chromium

# 5. Setup Systemd Service
echo "Configuring systemd service..."
# Copy the service file from the repo to the systemd directory
sudo cp north-scraper.service /etc/systemd/system/

# Reload systemd, enable, and start the service
sudo systemctl daemon-reload
sudo systemctl enable north-scraper.service
sudo systemctl restart north-scraper.service

echo "Deployment complete! The scraper is now running in the background."
echo "View live logs using: journalctl -u north-scraper -f"