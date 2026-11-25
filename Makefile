# Makefile

default:
	@echo "Choose a target."

vaillant-*.txt: vaillant-*.pdf
	@pdftotext $^ - | grep -v 'Downloaded from www' >$@

PKG := vaillant-boiler-ui
VERSION ?=
MAINTAINER ?= $(shell git config user.name) <$(shell git config user.email)>
DEB := build/$(PKG)_$(VERSION)_all.deb
DEBROOT := build/deb

.PHONY: deb
deb:
	@if [ -z '$(VERSION)' ]; then echo "Use make deb VERSION=X.Y.Z"; exit 1; fi
	@./make_deb.sh --pkg '$(PKG)' --version '$(VERSION)' --maintainer '$(MAINTAINER)' \
		--deb '$(DEB)' --debroot '$(DEBROOT)'

.PHONY: lint-deb
lint-deb: deb
	shellcheck make_deb.sh packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm
	@# verify a copy that runs the built program, as it isn't installed here
	install -d build/verify
	sed 's|/usr/bin/$(PKG)|$(CURDIR)/$(DEBROOT)/usr/bin/$(PKG)|' packaging/deb/$(PKG).service \
		>build/verify/$(PKG).service
	systemd-analyze verify build/verify/$(PKG).service

SH_FILES := make_deb.sh packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm

.PHONY: lint lint-sh lint-yaml lint-py lint-web
lint: lint-sh lint-yaml lint-py lint-web

lint-sh:
	shellcheck $(SH_FILES)

lint-yaml:
	yamllint --strict .

lint-py:
	black --check --diff app tests
	flake8 app tests
	mypy app/vaillant-boiler-ui.py app/vaillant_boiler_ui
	pylint app/vaillant-boiler-ui.py app/vaillant_boiler_ui

.PHONY: format
format:
	black app tests

.PHONY: test
test:
	python3 -m pytest -q tests

lint-web:
	npm run lint
