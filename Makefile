.PHONY: test
test:
	uv run tox

.PHONY: build
build:
	uvx --from build pyproject-build --installer uv

.PHONY: deploy
deploy:
	uvx twine upload dist/*
