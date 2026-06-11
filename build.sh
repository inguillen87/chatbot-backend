#!/usr/bin/env bash
# Exit on error
set -o errexit

# Install system dependencies for audio processing
apt-get update && apt-get install -y ffmpeg

# Install dependencies
pip install -r requirements.txt

if [ "${INSTALL_OPEN_SOURCE_AI_EXTRAS:-false}" = "true" ]; then
  pip install -r requirements-ai-oss.txt
fi

# SpaCy model is now installed via requirements.txt
