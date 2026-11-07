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

### app

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
