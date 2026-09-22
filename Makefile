.PHONY: start stop status logs wait-ready

CONFIG ?= conf_okx_multi.yml

start stop status logs wait-ready:
	@$(MAKE) -C hummingbot $@ CONFIG="$(CONFIG)"
