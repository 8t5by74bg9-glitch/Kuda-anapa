@echo off
py -m pip install -r requirements.txt
set SECRET_KEY=local-dev-secret
set ADMIN_LOGIN=admin
set ADMIN_PASSWORD=kuda-admin-2026
py -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
