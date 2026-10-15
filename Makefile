.DEFAULT_GOAL := help

.PHONY: help fix verify

help:
	@printf '%s\n' 'make fix     - Format source files'
	@printf '%s\n' 'make verify  - Check source quality'

fix:
	@uv run --locked ruff check --fix .
	@uv run --locked ruff format .

verify:
	@uv run --locked ruff check .
	@uv run --locked ruff format --check .
