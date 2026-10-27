# Starts backend (8000) and frontend (5173) in separate windows. Windows equivalent of `make backend` + `make frontend`.
$root = Split-Path -Parent $PSScriptRoot
Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root\backend'; python -m uvicorn app.main:app --reload --port 8000"
Start-Process powershell -ArgumentList "-NoExit","-Command","cd '$root\frontend'; npm run dev"
Write-Host "Backend: http://localhost:8000/docs   Frontend: http://localhost:5173"
