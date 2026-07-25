# kubernetes-install

Ansible-проект установки Kubernetes-кластера по документу
[`k8s-install-playbook-v2.md`](../k8s-install-playbook-v2.md) (v2.1).

Ansible отвечает за состояние узлов и базовую установку Kubernetes.
Всё, что внутри кластера после bootstrap, — зона ответственности Argo CD.

## Что нужно сделать перед первым запуском

1. **Проставить версии.** В `inventories/<cluster>/group_vars/all.yml` все
   значения `~` обязательны. Роль `preflight` падает на первой же непроставленной
   переменной — ошибка приходит за секунды, а не на этапе 30.

2. **Заменить плейсхолдеры контура.** `company.local`, `10.10.x.x` и адреса
   Harbor / GitLab / OpenBao — примеры. Меняются в `group_vars/all.yml`.

3. **Создать DNS-записи.** `k8s-api.<cluster-domain>` должен резолвиться
   в `api_vip` до запуска. Preflight это проверяет и отказывается работать,
   если запись указывает на адрес control-plane узла.

4. **Положить секреты в OpenBao** по пути `<openbao_cluster_path>`:

   | Ключ | Назначение |
   | ---- | ---------- |
   | `etcd-encryption-key` | Ключ шифрования Secrets, ровно 32 байта: `head -c 32 /dev/urandom \| base64` |
   | `gitops-deploy-key` | Read-only deploy key на GitOps-репозиторий |
   | `keepalived-auth-pass` | Пароль VRRP, максимум 8 символов |

5. **Подготовить GitOps-репозиторий.** Ansible берёт оттуда values для Cilium и
   Argo CD и манифест root Application — собственных шаблонов у него нет:

   ```
   clusters/<cluster>/values/cilium.yaml
   clusters/<cluster>/values/argocd.yaml
   clusters/<cluster>/bootstrap/root-application.yaml
   ```

   В `root-application.yaml` при первом bootstrap **не должно быть**
   `syncPolicy.automated` и finalizer `resources-finalizer.argocd.argoproj.io`.
   Ansible это проверяет и отказывается применять манифест.

## Запуск

```bash
make deps                       # коллекции и python-зависимости
make preflight                  # только проверки, ничего не меняет
make install                    # полная установка
make validate                   # валидация установленного кластера
```

Поэтапно:

```bash
ansible-playbook -i inventories/prod-01/hosts.yml playbooks/40-control-plane-init.yml
```

## Регламентные операции

```bash
# Добавить узел (сначала описать его в inventory)
ansible-playbook playbooks/maintenance/add-node.yml --limit k8s-worker-04

# Вывести узел; для control-plane удаляется и member etcd
ansible-playbook playbooks/maintenance/remove-node.yml -e node_to_remove=k8s-worker-04

# Продлить PKI control plane — не реже раза в 6 месяцев
ansible-playbook playbooks/maintenance/renew-control-plane-certs.yml

# Ротация ключа шифрования etcd: четыре фазы, по одной за запуск,
# с проверкой между ними
ansible-playbook playbooks/maintenance/rotate-etcd-encryption-key.yml -e rotation_phase=1
```

Обновление Kubernetes — `playbooks/upgrade/`, по порядку номеров.

## Тестирование

```bash
make lint                       # yamllint + ansible-lint + syntax-check
make test-role ROLE=kernel      # один контейнерный сценарий
make test-all-roles
make test-integration           # VM-прогон полного site.yml
```

Два уровня, как в разделе 25 документа:

* **Уровень 1 (docker)** — `preflight`, `kernel`, `os_prepare`, `containerd`,
  `kubernetes_packages`, `kubeadm_config`, `haproxy_keepalived`. Проверяется
  рендер конфигурации и идемпотентность. Запускаются на каждый MR.
* **Уровень 2 (delegated)** — роли, которым нужны реальное ядро, systemd и
  работающий кластер, плюс интеграционный `site-full`. Стенд поднимается
  через Vagrant + libvirt локально; для CI провайдер zVirt оставлен точкой
  расширения в `molecule-shared/resources/provision-zvirt.yml`.

Линтеры запускать **в версиях из `requirements-test.txt`**. На ansible-lint 6.x
проект даёт ложные ошибки: схема той версии не знает Ubuntu noble, а без
`netaddr` не работает фильтр `ipaddr` в роли `preflight`.

Сценарии, которым нужен GitOps-репозиторий, требуют переменных окружения:

```bash
export MOLECULE_GITOPS_REPO=ssh://git@gitlab.company.local/platform/kubernetes-gitops-test.git
export MOLECULE_REGISTRY_HOST=harbor.company.local
export MOLECULE_PROVIDER=vagrant
```

Тестовые секреты создаются из `secrets.yml.example` рядом со сценарием и в
репозиторий не коммитятся.

## Структура

```
inventories/          prod-01, stage-01
playbooks/            00..80, site.yml, upgrade/, maintenance/
roles/                13 ролей, каждая с molecule/default/
molecule-shared/      общие переменные и ресурсы для VM-стенда
molecule-integration/ прогон полного site.yml
```

Шаблоны лежат внутри ролей, а не в общем каталоге: иначе роль не
самодостаточна и molecule не может тестировать её в изоляции.

## Что важно знать при правках

* **Роли `kernel` и `os_prepare` не связаны meta-зависимостью.** Порядок задаёт
  playbook: `os_prepare` применяется ко всем узлам, включая LB, а kernel-тюнинг
  под Kubernetes — только узлам кластера.
* **Секреты разрешаются через `tasks/resolve_secrets.yml`** в каждой роли,
  которой они нужны. Файл намеренно продублирован: роль должна оставаться
  самодостаточной для molecule.
* **`cilium_bootstrap` и `argocd_bootstrap` проверяют владение Argo CD** перед
  применением манифестов. Если компонент уже под управлением Application
  в статусе `Synced`, роль пропускает apply целиком.
* **Форма вызова `helm template` собирается в одном месте** каждой bootstrap-роли.
  Формы `oci://` и `--repo` взаимоисключающие, и смешивание их — типичная ошибка.
* **`no_log: true` обязателен** для задач с токенами, ключами, kubeconfig и
  credentials. Molecule-сценарий `argocd_bootstrap` проверяет, что приватный
  ключ не утёк в логи и временные файлы.
