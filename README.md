# Vaillant Boiler Controller UI

This repo contains a back end and web front end to control a Vaillant Boiler.

## Support

It has been tested on:

| Vaillant boiler model | Supported |
| --------------------- | --------- |
| ecoTEC plus 837       | ✅ |
| all others            | ❌ |

## Installation

### ebusd

Get it from https://github.com/john30/ebusd.

Install with:

```sh
./autogen.sh
sudo make install-strip
sudo systemctl daemon-reload
sudo systemctl start ebusd
sudo systemctl enable ebusd
```

### app, from a DEB package

Build the package (needs `dpkg-deb`; `make lint-deb` also needs `shellcheck`):

```sh
make deb                  # or: make deb VERSION=0.2.0
scp build/vaillant-boiler-ui_*_all.deb "$SERVER_USER@$SERVER_ADDR:"
```

On the Pi:

```sh
sudo apt install ./vaillant-boiler-ui_*_all.deb
```

On first install it creates a login `admin` (role admin) with a random password, prints the
password once, and starts the service. Change the password, or add more users, with:

```sh
sudo htpasswd -B -C 6 /etc/vaillant-boiler-ui/users.htpasswd admin
sudo nano /etc/vaillant-boiler-ui/roles     # roles for any new users
```

The printed password is also kept in apt's log, `/var/log/apt/term.log`, so change it after installing.

The package installs:

| Path | Contents |
| ---- | -------- |
| `/usr/bin/vaillant-boiler-ui` | the program |
| `/usr/lib/systemd/system/vaillant-boiler-ui.service` | service, enabled on install; it waits until `users.htpasswd` has a user |
| `/etc/vaillant-boiler-ui/users.htpasswd`, `roles` | logins (root-owned, readable by the service only) |
| `/etc/default/vaillant-boiler-ui` | extra options in `BOILER_UI_OPTS` |
| `/var/lib/vaillant-boiler-ui/` | CSV logs, summaries, change log and token signing key |

It runs as the `boiler-ui` system user. `sudo apt purge vaillant-boiler-ui` deletes the logins and all logged data.

### app, copied by hand

Copy `.env.txt` to `.env` and set your Raspberry Pi server username (`SERVER_USER`), address (`SERVER_ADDR`) and install path (`SERVER_PATH`).

```sh
source .env
make install

ssh "$SERVER_USER@$SERVER_ADDR"
  # -c: create the file, use it first time only
  htpasswd -c -B -C 6 boiler-users.htpasswd YOUR_ADMIN_USER
  echo 'admin: YOUR_ADMIN_USER' >boiler-roles
  chmod 600 boiler-users.htpasswd boiler-roles
  # install systemd file, then:
  sudo systemctl daemon-reload
  sudo systemctl start boiler-ui.service
  sudo systemctl enable boiler-ui.service
  exit # log out of ssh
```

Now check out the server at `http://$SERVER_ADDR:8000/`.

## Hardware

- Raspberry Pi Zero with USB hub (in the data port) and wifi dongle
- [eBUS Adapter Shield C6](https://adapter.ebusd.eu/v5-c6/index.en.html)

![Pi Zero plus eBUS C6](pizero.png)
