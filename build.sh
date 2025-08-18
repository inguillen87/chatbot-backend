#!/usr/bin/env bash
# Exit on error
set -o errexit

# Install system dependencies for audio processing
apt-get update && apt-get install -y ffmpeg

# Install dependencies
pip install -r requirements.txt

# SpaCy model is now installed via requirements.txt
