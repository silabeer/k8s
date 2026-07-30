INVENTORY ?= inventories/prod-01/hosts.yml

.PHONY: help deps lint syntax preflight install validate clean

help:
	@echo "deps             — установить коллекции и python-зависимости"
	@echo "lint             — yamllint + ansible-lint"
	@echo "syntax           — синтаксическая проверка всех playbook"
	@echo "preflight        — только проверки, без изменений"
	@echo "install          — полная установка кластера"
	@echo "validate         — валидация установленного кластера"

deps:
	ansible-galaxy collection install -r requirements.yml -p collections
	pip install -r requirements-python.txt -r requirements-test.txt

lint:
	yamllint .
	ansible-lint
	python3 scripts/check-assert-conditions.py

# Молчаливая опечатка в import_playbook ловится только здесь, до выезда на стенд.
syntax:
	@for p in playbooks/*.yml playbooks/upgrade/*.yml playbooks/maintenance/*.yml; do \
		echo "== $$p"; \
		ansible-playbook -i $(INVENTORY) --syntax-check $$p || exit 1; \
	done

preflight:
	ansible-playbook -i $(INVENTORY) playbooks/00-preflight.yml

install:
	ansible-playbook -i $(INVENTORY) playbooks/site.yml

validate:
	ansible-playbook -i $(INVENTORY) playbooks/70-validation.yml

clean:
	rm -rf .ansible_facts artifacts
	find . -name '*.retry' -delete
