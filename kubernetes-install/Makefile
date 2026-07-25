INVENTORY ?= inventories/prod-01/hosts.yml
ROLE      ?=

.PHONY: help deps lint syntax test-role test-all-roles test-integration \
        preflight install validate clean

help:
	@echo "deps             — установить коллекции и python-зависимости"
	@echo "lint             — yamllint + ansible-lint"
	@echo "syntax           — синтаксическая проверка всех playbook"
	@echo "test-role ROLE=x — molecule test одной роли"
	@echo "test-all-roles   — molecule test всех ролей, где есть сценарий"
	@echo "test-integration — VM-прогон полного site.yml"
	@echo "preflight        — только проверки, без изменений"
	@echo "install          — полная установка кластера"
	@echo "validate         — валидация установленного кластера"

deps:
	ansible-galaxy collection install -r requirements.yml -p collections
	pip install -r requirements-test.txt

lint:
	yamllint .
	ansible-lint

# Молчаливая опечатка в import_playbook ловится только здесь, до выезда на стенд.
syntax:
	@for p in playbooks/*.yml playbooks/upgrade/*.yml playbooks/maintenance/*.yml; do \
		echo "== $$p"; \
		ansible-playbook -i $(INVENTORY) --syntax-check $$p || exit 1; \
	done

test-role:
	@test -n "$(ROLE)" || (echo "Укажите ROLE=<имя роли>" && exit 1)
	cd roles/$(ROLE) && molecule test

test-all-roles:
	@for r in roles/*/; do \
		if [ -d "$$r/molecule" ]; then \
			echo "== $$r"; \
			(cd $$r && molecule test) || exit 1; \
		fi; \
	done

test-integration:
	cd molecule-integration && molecule test -s site-full

preflight:
	ansible-playbook -i $(INVENTORY) playbooks/00-preflight.yml

install:
	ansible-playbook -i $(INVENTORY) playbooks/site.yml

validate:
	ansible-playbook -i $(INVENTORY) playbooks/70-validation.yml

clean:
	rm -rf .ansible_facts artifacts
	find . -name '*.retry' -delete
