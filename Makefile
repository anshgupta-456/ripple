.PHONY: backend frontend test types

backend:
	cd backend && python -m uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

test:
	cd backend && python -m pytest -q

types:
	cd frontend && npm run gen:types
