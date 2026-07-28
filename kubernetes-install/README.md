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
make lint      # yamllint + ansible-lint + check-assert-conditions
make syntax    # синтаксическая проверка всех playbook
```

Контейнерных сценариев в проекте нет: проверка идёт статическим анализом
на каждый MR и прогоном на стенде из виртуальных машин.

`scripts/check-assert-conditions.py` написан по следам конкретного отказа:
`: ` внутри незакавыченного условия `assert` превращает его в отображение
YAML, условие молча перестаёт проверяться, и ни yamllint, ни ansible-lint,
ни `--syntax-check` этого не ловят.

### Стенд

Минимум — три control-plane и два worker; пятый узел держится вне inventory
и вводится через `add-node`, чтобы проверить расширение.

Обязательный набор перед выпуском изменений: `site.yml` целиком,
`add-node`, `remove-node`, `renew-control-plane-certs`,
`rotate-etcd-encryption-key` (все четыре фазы), `80-backup.yml` с ожиданием
срабатывания таймера, `restore-etcd`, `upgrade/` по порядку номеров.

Результат проверяется на живом кластере, а не по коду возврата Ansible:
узлы `Ready`, кворум etcd, префикс шифрования записей в etcd, возврат
манифестов статических подов, отсутствие подов вне `Running`.

Ротация ключа и восстановление без контрольных объектов не считаются
проверенными. Для ротации — секрет, записанный до неё и прочитанный после.
Для восстановления — объекты, созданные ПОСЛЕ снятия снимка: если они не
исчезли, восстановление не состоялось.

### Чего облачный стенд не проверяет

| Что | Препятствие |
| --- | ----------- |
| `haproxy_keepalived` вживую | произвольный VIP на L2 в облаке не поднять |
| отдельный диск под etcd | нужна ВМ с дополнительным томом |
| внутренние зеркала и Harbor | нужен закрытый контур |
| OpenBao как секрет-бэкенд | нужен OpenBao |
| приватный GitOps по SSH | нужен deploy key |
| отказные пути `preflight` | прогон идёт по счастливому пути |

Линтеры запускать **в версиях из `requirements-test.txt`**. На ansible-lint
6.x проект даёт ложные ошибки: схема той версии не знает Ubuntu noble, а без
`netaddr` не работает фильтр `ipaddr` в роли `preflight`.

## Структура

```
inventories/          prod-01, stage-01
playbooks/            00..80, site.yml, upgrade/, maintenance/
roles/                13 ролей
scripts/              вспомогательные проверки
```

Шаблоны лежат внутри ролей, а не в общем каталоге: с общим каталогом роль
перестаёт быть самодостаточной и её нельзя применить отдельно от проекта.

## Что важно знать при правках

* **Роли `kernel` и `os_prepare` не связаны meta-зависимостью.** Порядок задаёт
  playbook: `os_prepare` применяется ко всем узлам, включая LB, а kernel-тюнинг
  под Kubernetes — только узлам кластера.
* **Секреты разрешаются через `tasks/resolve_secrets.yml`** в каждой роли,
  которой они нужны. Файл намеренно продублирован: роль должна оставаться
  самодостаточной и применимой отдельно.
* **`cilium_bootstrap` и `argocd_bootstrap` проверяют владение Argo CD** перед
  применением манифестов. Если компонент уже под управлением Application
  в статусе `Synced`, роль пропускает apply целиком.
* **Форма вызова `helm template` собирается в одном месте** каждой bootstrap-роли.
  Формы `oci://` и `--repo` взаимоисключающие, и смешивание их — типичная ошибка.
* **`no_log: true` обязателен** для задач с токенами, ключами, kubeconfig и
  credentials — но только для тех, что действительно печатают секрет.
  На проверках формата он вреден: скрывает причину отказа и превращает
  диагностику в угадывание. Дважды приводил к этому на живом кластере.
  Автоматической проверки на утечку ключа в логи больше нет — она жила в
  удалённом контейнерном сценарии `argocd_bootstrap`.
* **`node_ip`, а не `ansible_host`,** во всём, что касается адреса узла
  в кластере: `--node-ip`, `advertiseAddress`, `certSANs`, backend HAProxy,
  `unicast_peer` keepalived, `/etc/hosts`. `ansible_host` — адрес подключения,
  и при `connection: docker` он равен имени контейнера. В production-inventory
  `node_ip: "{{ ansible_host }}"`.
