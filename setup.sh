#!/bin/bash
set -e
echo "Setting up Trading Platform..."
cd "$(dirname "$0")"
mkdir -p output/pending_prompts
pip install -r requirements.txt
python3 -c "from storage.database import Database; Database()"
chmod +x run.sh
echo "Done. Run: ./run.sh"
