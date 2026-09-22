.PHONY: start stop status logs wait-ready dashboard stop-dashboard

CONFIG ?= conf_okx_multi.yml

start stop status logs wait-ready dashboard stop-dashboard:
	@$(MAKE) -C hummingbot $@ CONFIG="$(CONFIG)"
