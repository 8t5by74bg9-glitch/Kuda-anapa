#!/bin/bash
set -e
python3 -m pip install -r requirements.txt
export SECRET_KEY="${SECRET_KEY:-local-dev-secret}"
export ADMIN_LOGIN="${ADMIN_LOGIN:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-kuda-admin-2026}"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
