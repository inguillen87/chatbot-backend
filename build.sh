#!/usr/bin/env bash
# Exit on error
set -o errexit

# Install dependencies
pip install -r requirements.txt

# Download SpaCy model
python -m spacy download es_core_news_md
