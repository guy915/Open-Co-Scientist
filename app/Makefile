.PHONY: install dev start test format lint typecheck

install:
	pip install -e "../engine[dev]"

dev:
	python -m uvicorn co_scientist.main:app --reload --reload-dir ../engine/src --host 0.0.0.0 --port 8008

start:
	python -m uvicorn co_scientist.main:app --host 0.0.0.0 --port 8008

test:
	pytest

format:
	ruff format tests

lint:
	ruff check tests

typecheck:
	mypy .
