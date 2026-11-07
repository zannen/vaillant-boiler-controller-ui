# Makefile

default: nothing

nothing:
	@echo "Choose a target."

vaillant-*.txt: vaillant-*.pdf
	@pdftotext $^ - | grep -v 'Downloaded from www' >$@

install:
	@source .env && scp app/boiler-log-ui.py $(SERVER_USER)@$(SERVER_ADDR):$(SERVER_PATH)
