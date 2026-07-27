INVENTORY ?= inventories/prod-01/hosts.yml
ROLE      ?=

.PHONY: help deps lint syntax test-role test-all-roles test-integration \
        ci-host-prereq preflight install validate clean

help:
	@echo "deps             — установить коллекции и python-зависимости"
	@echo "lint             — yamllint + ansible-lint"
	@echo "syntax           — синтаксическая проверка всех playbook"
	@echo "test-role ROLE=x — molecule test одной роли"
	@echo "test-all-roles   — molecule test всех ролей, где есть сценарий"
	@echo "test-integration — прогон полного site.yml на docker-стенде"
	@echo "ci-host-prereq   — sysctl на хосте CI для строгого режима kubelet"
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

# kernel.* не изолируются namespace: внутри контейнера kubelet видит значения
# ХОСТА, а Docker не даёт задать их через `sysctls`. Чтобы кластерные сценарии
# работали с protectKernelDefaults: true, эти значения должны быть выставлены
# на самом хосте CI.
#
# Выполнять только на выделенном раннере: значения влияют на весь хост.
ci-host-prereq:
	@echo "Выставляю sysctl, требуемые protectKernelDefaults, на ЭТОМ хосте:"
	sudo sysctl -w vm.overcommit_memory=1
	sudo sysctl -w vm.panic_on_oom=0
	sudo sysctl -w kernel.panic=10
	sudo sysctl -w kernel.panic_on_oops=1
	sudo sysctl -w kernel.keys.root_maxkeys=1000000
	sudo sysctl -w kernel.keys.root_maxbytes=25000000
	@echo
	@echo "Теперь запускайте сценарии с MOLECULE_HOST_SYSCTLS_SET=true"

preflight:
	ansible-playbook -i $(INVENTORY) playbooks/00-preflight.yml

install:
	ansible-playbook -i $(INVENTORY) playbooks/site.yml

validate:
	ansible-playbook -i $(INVENTORY) playbooks/70-validation.yml

clean:
	rm -rf .ansible_facts artifacts
	find . -name '*.retry' -delete
