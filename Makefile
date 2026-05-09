all: test mypy black ruff

.PHONY: test
test:
	pytest

.PHONY: black
black:
	black src tests

.PHONY: mypy
mypy:
	mypy src tests

.PHONY: ruff
ruff:
	ruff check src tests

.PHONY: package
package:
	python3 -m build --sdist --wheel
