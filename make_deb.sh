#!/bin/bash
# Build the Debian package from the files in this repository.
#
# Usage: make_deb.sh --pkg NAME --version VERSION --maintainer "Name <email>" --deb OUT.deb --debroot DIR

set -euo pipefail

usage() {
	echo "usage: $0 --pkg NAME --version VERSION --maintainer \"Name <email>\" --deb OUT.deb --debroot DIR" >&2
	exit 2
}

pkg=""
version=""
maintainer=""
deb=""
debroot=""
while [ $# -gt 0 ]; do
	[ $# -ge 2 ] || usage
	case "$1" in
	--pkg) pkg="$2" ;;
	--version) version="$2" ;;
	--maintainer) maintainer="$2" ;;
	--deb) deb="$2" ;;
	--debroot) debroot="$2" ;;
	*) usage ;;
	esac
	shift 2
done
for value in "$pkg" "$version" "$maintainer" "$deb" "$debroot"; do
	[ -n "$value" ] || usage
done

# paths below are relative to the repository root
cd "$(dirname "$0")"

rm -rf "$debroot" "$deb"
install -d "$debroot/DEBIAN" "$debroot/usr/bin" "$debroot/usr/lib/$pkg/static" \
	"$debroot/usr/lib/systemd/system" "$debroot/usr/share/$pkg" \
	"$debroot/usr/share/doc/$pkg" "$debroot/etc/default"
sed -e "s/@VERSION@/$version/" -e "s/@MAINTAINER@/$maintainer/" \
	packaging/deb/control >"$debroot/DEBIAN/control"
install -m 0644 packaging/deb/conffiles "$debroot/DEBIAN/"
install -m 0755 packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm "$debroot/DEBIAN/"
install -m 0755 "app/$pkg.py" "$debroot/usr/lib/$pkg/"
install -m 0644 app/static/* "$debroot/usr/lib/$pkg/static/"
ln -s "../lib/$pkg/$pkg.py" "$debroot/usr/bin/"
install -m 0644 "packaging/deb/$pkg.service" "$debroot/usr/lib/systemd/system/"
install -m 0644 packaging/deb/default "$debroot/etc/default/$pkg"
install -m 0644 packaging/deb/roles.example "$debroot/usr/share/$pkg/"
install -m 0644 LICENSE "$debroot/usr/share/doc/$pkg/copyright"
install -m 0644 README.md "$debroot/usr/share/doc/$pkg/"
dpkg-deb --root-owner-group -Zxz --build "$debroot" "$deb"
echo "built $deb"
