.PHONY: check up test e2e down secrets-check

P01 := projects/p01-lead-automation

check: secrets-check
	$(MAKE) -C $(P01) check

up test e2e down:
	$(MAKE) -C $(P01) $@

secrets-check:
	$(P01)/scripts/check_secrets.sh
