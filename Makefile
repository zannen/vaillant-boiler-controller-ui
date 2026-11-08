# Makefile

default: nothing

nothing:
	@echo "Choose a target."

vaillant-*.txt: vaillant-*.pdf
	@pdftotext $^ - | grep -v 'Downloaded from www' >$@

install:
	@source .env && scp app/boiler-log-ui.py $(SERVER_USER)@$(SERVER_ADDR):$(SERVER_PATH)

PKG := vaillant-boiler-ui
VERSION ?= 0.1.0
MAINTAINER ?= $(shell git config user.name) <$(shell git config user.email)>
DEB := build/$(PKG)_$(VERSION)_all.deb
DEBROOT := build/deb

.PHONY: deb
deb:
	rm -rf $(DEBROOT) $(DEB)
	install -d $(DEBROOT)/DEBIAN $(DEBROOT)/usr/bin $(DEBROOT)/usr/lib/$(PKG) \
		$(DEBROOT)/usr/lib/systemd/system $(DEBROOT)/usr/share/$(PKG) \
		$(DEBROOT)/usr/share/doc/$(PKG) $(DEBROOT)/etc/default
	sed -e 's/@VERSION@/$(VERSION)/' -e 's/@MAINTAINER@/$(MAINTAINER)/' \
		packaging/deb/control >$(DEBROOT)/DEBIAN/control
	install -m 0644 packaging/deb/conffiles $(DEBROOT)/DEBIAN/
	install -m 0755 packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm $(DEBROOT)/DEBIAN/
	install -m 0755 app/boiler-log-ui.py $(DEBROOT)/usr/lib/$(PKG)/
	ln -s ../lib/$(PKG)/boiler-log-ui.py $(DEBROOT)/usr/bin/$(PKG)
	install -m 0644 packaging/deb/$(PKG).service $(DEBROOT)/usr/lib/systemd/system/
	install -m 0644 packaging/deb/default $(DEBROOT)/etc/default/$(PKG)
	install -m 0644 packaging/deb/roles.example $(DEBROOT)/usr/share/$(PKG)/
	install -m 0644 LICENSE $(DEBROOT)/usr/share/doc/$(PKG)/copyright
	install -m 0644 README.md $(DEBROOT)/usr/share/doc/$(PKG)/
	dpkg-deb --root-owner-group -Zxz --build $(DEBROOT) $(DEB)
	@echo "built $(DEB)"

.PHONY: lint-deb
lint-deb: deb
	shellcheck packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm
	@# verify a copy that runs the built program, as it isn't installed here
	install -d build/verify
	sed 's|/usr/bin/$(PKG)|$(CURDIR)/$(DEBROOT)/usr/bin/$(PKG)|' packaging/deb/$(PKG).service \
		>build/verify/$(PKG).service
	systemd-analyze verify build/verify/$(PKG).service
