# Дизайн Ansible-проекта `kubernetes-install`

Дата: 2026-07-25
Источник требований: `k8s-install-playbook-v2.md` (v2.0) + ревью, зафиксированное в этом документе.

## 1. Цель

Реализовать Ansible-проект, поднимающий Kubernetes-кластер по playbook v2, с исправлением
дефектов, найденных при ревью документа. Документ обновляется параллельно до v2.1, чтобы
код и спецификация не расходились.

## 2. Дефекты v2.0, исправляемые в реализации

Нумерация соответствует ревью. Каждый пункт — обязательное требование к коду.

### Блокеры

**B1. Неполный набор sysctl для `protectKernelDefaults: true`.**
Kubelet проверяет шесть значений и падает при несовпадении, а не выставляет их сам.
Роль `kernel` обязана выставлять все шесть:

```
vm.overcommit_memory      = 1
vm.panic_on_oom           = 0
kernel.panic              = 10
kernel.panic_on_oops      = 1
kernel.keys.root_maxkeys  = 1000000   # дефолт 200
kernel.keys.root_maxbytes = 25000000  # дефолт 20000
```

Последние два отличаются от дефолтов Linux, без них kubelet не стартует на каждом узле.
`verify.yml` роли `kernel` проверяет фактические значения через `/proc/sys`.

**B2. Kubelet не может self-назначить `node-role.kubernetes.io/worker`.**
Admission-плагин `NodeRestriction` запрещает kubelet выставлять labels в домене
`kubernetes.io` вне короткого whitelist. `kubeadm join` с таким `--node-labels` падает.
Роль `node_join` передаёт через `nodeRegistration.kubeletExtraArgs` только
бездоменные labels (`node-pool`), а `node-role.kubernetes.io/worker` назначает
отдельной задачей `kubernetes.core.k8s` с `delegate_to` на первый control-plane узел.

**B3. Некорректная форма вызова `helm template`.**
С флагом `--repo` имя чарта указывается без префикса репозитория. Реализация
поддерживает два режима через `helm_chart_source: repo|oci` и собирает команду
в одном месте (`tasks/helm_render.yml`), чтобы форма не расходилась между ролями.

**B4. Ротация ключа шифрования etcd описана в неверном порядке.**
Корректная последовательность в HA — четыре шага: (1) новый ключ вторым провайдером +
рестарт всех apiserver; (2) новый ключ первым + рестарт всех; (3) перешифровка;
(4) удаление старого ключа + рестарт. Оформляется отдельным playbook
`playbooks/maintenance/rotate-etcd-encryption-key.yml`.

### Существенные

**S5. `aescbc` не рекомендуется.** Дефолт реализации — `secretbox`; `aescbc` и `kms`
остаются доступны через `etcd_encryption_provider`. При `kms` шаблон рендерит
конфигурацию KMS v2 под OpenBao.

**S6. TCP-only health check в HAProxy.** Шаблон использует
`option httpchk GET /readyz` + `http-check expect status 200` + `check check-ssl verify none`.

**S7. Taints выделенных пулов — при регистрации узла.** `nodeRegistration.taints`
в JoinConfiguration вместо назначения через GitOps: узел не существует в кластере
в «незатейнченном» состоянии.

**S8. Root Application не самоуправляем.** Ansible применяет ровно тот манифест,
который лежит в GitOps-репозитории (`clusters/<cluster>/bootstrap/root-application.yaml`),
и не имеет собственного шаблона. Перевод в AppProject `platform-root` выполняется
изменением в Git, а не Ansible.

**S9. `prune`/`selfHeal` на root Application при bootstrap.** Ansible применяет root
Application без блока `automated`. Включение автоматики — отдельный осознанный шаг
(переменная `argocd_root_app_automated: false` по умолчанию).

**S10. Отсутствует регламент продления PKI control plane.** Добавляются:
роль-независимый playbook `playbooks/maintenance/renew-control-plane-certs.yml`
и проверка срока в `cluster_validation` (предупреждение при < 90 дней).

**S11. `evictionHard` перетирает дефолты.** В набор добавляется `nodefs.inodesFree: 5%`.

**S12. Мелочи, ломающиеся в проде.**
- etcd-backup через hostPath требует `hostNetwork: true` — отражено в шаблоне CronJob;
- отдельный диск etcd монтируется в `/var/lib/etcd` ролью `os_prepare` до `kubeadm init`;
- в preflight добавлены порты 4245 (hubble-relay) и диапазон NodePort 30000-32767.

**S13. Пример `molecule.yml` невалиден для Molecule 6+.** Ключ `lint:` не используется;
линтеры запускаются из Makefile и CI.

### Дополнительно найденное при реализации

**S14. `environment` как имя переменной.** В §6 документа предлагается
`environment: prod`. `environment` — зарезервированное ключевое слово Ansible на уровне
play/task; переменная с таким именем создаёт трудноуловимые конфликты.
Переименована в `cluster_environment`.

**S15. Отсутствует Pod Security Admission.** Между `kubeadm init` и установкой
policy engine (wave 0) кластер ничем не защищён. Добавляется
`AdmissionConfiguration` с плагином `PodSecurity` и baseline-дефолтом,
подключаемый через `apiServer.extraArgs.admission-control-config-file`.

**S16. Отсутствует TLS-политика.** Добавляются `--tls-min-version` и
`--tls-cipher-suites` для apiserver, etcd и kubelet.

## 3. Архитектурные решения реализации

### 3.1. Границы ролей `kernel` и `os_prepare`

В §7 v2.0 обе роли «настраивают sysctl». Развод ответственности:

| Роль | Владеет |
|---|---|
| `kernel` | `modules-load.d`, `sysctl.d` (сеть + полный набор `protectKernelDefaults`), загрузка и верификация модулей |
| `os_prepare` | hostname, DNS, `/etc/hosts`, базовые пакеты, NTP, swap, firewall, logrotate, корневые CA, монтирование диска etcd |

Роли намеренно **не** связаны meta-зависимостью: `os_prepare` применяется ко всем
узлам, включая LB, и `dependencies: [kernel]` протащил бы kernel-тюнинг под
Kubernetes на балансировщики. Порядок задаёт `playbooks/10-os-prepare.yml`,
где `kernel` применяется только к группе `kubernetes`.

### 3.2. Разрешение секретов

Роли не знают источник секретов. Каждая подключает `tasks/resolve_secrets.yml`,
выбирающий бэкенд по `secret_backend`:

- `openbao` — `community.hashi_vault.hashi_vault` lookup (production);
- `vault_file` — ansible-vault файл (локальная разработка, molecule).

Секреты: `etcd_encryption_key`, `gitops_deploy_key`, `argocd_admin_password`.
Все задачи с ними — под `no_log: true`.

### 3.3. Защита от повторного применения после takeover

`cilium_bootstrap` и `argocd_bootstrap` разделяют `tasks/assert_not_argocd_owned.yml`:
запрашивается Argo CD `Application`; если он существует и `Synced`, роль пропускает
apply целиком и выставляет факт для отчёта.

### 3.4. Fail-fast на непроставленных переменных

`group_vars/all.yml` содержит версии как `~` (null) с комментарием. Роль `preflight`
первой задачей делает `assert` на весь обязательный набор. Ошибка приходит за секунды,
а не на этапе 30.

### 3.5. Molecule уровня 2

Оба уровня используют driver `docker`. Кластерные сценарии поднимают kubeadm
внутри контейнеров — тот же приём, что у `kind`: `privileged`, `cgroupns_mode:
host`, `/lib/modules` только на чтение, проброшенный `/dev/kmsg` и анонимный
том на `/var/lib/containerd` (overlayfs поверх overlayfs не работает).

Сеть и имена контейнеров уникальны для каждого сценария: `molecule destroy`
удаляет сеть, и общая сеть у параллельных сценариев в CI даёт гонку.

Три вещи контейнер проверить не позволяет; они выключены явно в общем файле
переменных и закрыты другими проверками: фактические sysctl
`protectKernelDefaults` (`kernel.*` не изолируются namespace), отключение swap
(`/proc/swaps` принадлежит хосту) и отдельный диск под etcd. Для первого
предусмотрен строгий режим через `make ci-host-prereq` и переменную
`MOLECULE_HOST_SYSCTLS_SET`.

### 3.7. Разделение адреса подключения и адреса узла

При `connection: docker` переменная `ansible_host` — имя контейнера, поэтому
адрес узла в кластере вынесен в `node_ip`. Она используется в `--node-ip`,
`advertiseAddress`, `certSANs`, backend HAProxy, `unicast_peer` keepalived и
`/etc/hosts`. В production-inventory `node_ip: "{{ ansible_host }}"`.

Разделение оправдано и вне тестов: сеть управления и кластерная сеть совпадают
не всегда, а подключение может идти через bastion.

### 3.6. Расположение шаблонов

Отклонение от §7 v2.0: шаблоны лежат в `roles/<role>/templates/`, а не в общем
каталоге `templates/`. Общий каталог не виден изолированным molecule-сценариям ролей.
Документ правится соответственно.

## 4. Состав поставки

```
kubernetes-install/
├── ansible.cfg, requirements.yml, requirements-test.txt
├── Makefile, .gitlab-ci.yml, .yamllint, .ansible-lint, README.md
├── inventories/{prod-01,stage-01}/
├── playbooks/            00..80, site.yml, upgrade/, maintenance/
├── roles/                13 ролей, каждая с molecule/default/
└── molecule-integration/site-full/
```

## 5. Тестирование

- **Уровень 1 (docker)**: `preflight`, `kernel`, `os_prepare`, `containerd`,
  `kubernetes_packages`, `kubeadm_config`, `haproxy_keepalived`.
  Проверяется рендер конфигурации и идемпотентность.
- **Уровень 2 (delegated)**: `control_plane_init`, `node_join`, `cilium_bootstrap`,
  `argocd_bootstrap`, `cluster_validation`, `etcd_backup` и интеграционный `site-full`.
- Шаг `molecule idempotence` обязателен для всех сценариев.

Ключевые проверки verify перечислены в §25.4 документа и реализуются как есть,
с добавлением: `kernel` проверяет фактические значения шести sysctl (B1),
`node_join` проверяет наличие label `node-role.kubernetes.io/worker` (B2).
