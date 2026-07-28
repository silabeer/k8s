# Playbook установки Kubernetes и первоначальной настройки кластера

Версия 2.1. Учтены результаты валидации: аппрув kubelet serving CSR, sysctl для `protectKernelDefaults`, роль load balancer, шифрование Secrets в etcd, audit policy apiserver, процедура передачи Cilium под Argo CD, resource reservation kubelet, containerd 2.x registry config, регламент etcd snapshot, стратегия taints/labels, архитектурное решение по топологии Argo CD, тестирование.

## Изменения версии 2.1

Версия исправляет дефекты, найденные при реализации Ansible-проекта. Три из них блокировали установку.

**Блокеры**

* **Неполный набор sysctl для `protectKernelDefaults`** (раздел 11). Перечислялись три значения из шести; без `kernel.keys.root_maxkeys` и `kernel.keys.root_maxbytes` kubelet не стартует ни на одном узле.
* **Label `node-role.kubernetes.io/worker` через `--node-labels`** (раздел 16). Admission-плагин `NodeRestriction` это запрещает, `kubeadm join` завершается ошибкой. Роль узла назначается после join от имени cluster-admin.
* **Некорректная форма вызова `helm template`** (разделы 4.3, 17). Смешаны две взаимоисключающие формы указания чарта.

**Существенные изменения**

* Процедура ротации ключа шифрования etcd переписана: в HA требуется четыре фазы, а не три (раздел 14.1).
* Дефолтный провайдер шифрования — `secretbox` вместо `aescbc` (раздел 14.1).
* Health check HAProxy — HTTP `/readyz` вместо голой TCP-проверки (раздел 10).
* Taints выделенных пулов выставляются при регистрации узла, а не через GitOps (раздел 3.3).
* Root Application берётся из GitOps-репозитория и не имеет `automated` при первом bootstrap (раздел 18.3).
* Добавлен регламент продления PKI control plane (раздел 27.1) — сертификаты kubeadm живут один год.
* Добавлены Pod Security Admission и TLS-политика в `kubeadm init` (раздел 14.2).
* `evictionHard` дополнен `nodefs.inodesFree` (раздел 14.2).
* Переменная `environment` переименована в `cluster_environment` — исходное имя зарезервировано Ansible (раздел 6).
* Шаблоны перенесены в каталоги ролей (раздел 7).
* Тестирование целиком переведено на driver `docker`: кластерные сценарии поднимают kubeadm внутри контейнеров, зависимость от гипервизора и VM-стенда убрана. Введена переменная `node_ip`, отделяющая адрес узла в кластере от адреса подключения (раздел 25.3).

## 1. Назначение документа

Документ описывает процесс создания Kubernetes-кластера на виртуальных машинах с использованием:

* Ansible для подготовки операционной системы и установки Kubernetes;
* kubeadm для инициализации control plane и присоединения узлов;
* containerd в качестве container runtime;
* Cilium в качестве CNI;
* Argo CD для последующей декларативной настройки кластера;
* Git как единственного источника целевого состояния кластерных компонентов.

Основная цель процесса — получить воспроизводимую установку кластера, при которой ручные изменения минимизированы, а вся конфигурация после bootstrap хранится и изменяется через Git.

---

# 2. Основные принципы

## 2.1. Разделение ответственности

Процесс установки делится на два слоя.

### Слой 1. Infrastructure и Kubernetes bootstrap

Управляется Ansible.

Включает:

* подготовку виртуальных машин;
* настройку операционной системы;
* настройку сети и DNS;
* настройку Load Balancer для API endpoint (HAProxy + Keepalived), если не используется внешний корпоративный LB;
* установку containerd;
* установку kubelet, kubeadm и kubectl;
* создание control plane;
* присоединение дополнительных control-plane узлов;
* присоединение worker-узлов;
* bootstrap CNI;
* bootstrap Argo CD;
* передачу управления кластером GitOps-контуру.

### Слой 2. Cluster configuration

Управляется Argo CD.

Включает:

* конфигурацию самого Argo CD;
* декларативное управление CNI (после takeover);
* kubelet-csr-approver;
* ingress или Gateway API;
* CSI-драйверы и StorageClass;
* cert-manager;
* External Secrets Operator;
* observability-агенты;
* policy engine;
* default namespaces;
* ResourceQuota и LimitRange;
* NetworkPolicy;
* RBAC;
* platform operators;
* системные приложения;
* конфигурацию окружений.

Ansible не должен становиться альтернативным Kubernetes-оператором. После запуска GitOps Ansible используется только для управления узлами и жизненным циклом базового Kubernetes.

## 2.2. Топология Argo CD (архитектурное решение)

Для production-кластеров используется **in-cluster Argo CD (per-cluster)**: каждый prod-кластер содержит собственный экземпляр Argo CD, управляющий кластером через `https://kubernetes.default.svc`.

Обоснование:

* отсутствие cluster-admin credentials, пересекающих границы кластеров и сетевых сегментов;
* отказ или компрометация одного Argo CD затрагивает один кластер;
* не требуется сетевая связность между сегментами до API endpoints;
* восстановление кластера из Git автономно и не зависит от внешнего management-кластера.

Централизованный (hub-and-spoke) Argo CD допускается только для non-prod окружений (dev, ephemeral-кластеры) и оформляется отдельным ADR.

Единообразие N инсталляций обеспечивается тем, что конфигурация Argo CD сама управляется через GitOps (wave -20): один шаблон в `infrastructure/argocd/`, кластер-специфичны только values-оверрайды. Обновление Argo CD — один PR, раскатываемый сначала на stage, затем на prod.

Единая видимость всех кластеров обеспечивается без централизации управления:

* метрики Argo CD (`argocd_app_info` и др.) собираются в VictoriaMetrics;
* общий дашборд Sync/Health-статусов всех Applications всех кластеров;
* Argo CD Notifications в общий канал.

---

# 3. Целевая архитектура

## 3.1. Состав кластера

Для production-кластера рекомендуется следующий минимальный состав:

| Роль                  |                   Количество | Назначение                                          |
| --------------------- | ---------------------------: | --------------------------------------------------- |
| Load balancer (VIP)   |         2 или внешний сервис | Единая точка входа в Kubernetes API                 |
| Control plane         |                            3 | kube-apiserver, controller-manager, scheduler, etcd |
| Worker                |                           3+ | Запуск пользовательских workload                    |
| Git repository        |                           1+ | Источник конфигурации кластера                      |
| Argo CD               |     in-cluster, HA установка | Синхронизация Git с Kubernetes                      |

Для control plane применяется topology `stacked etcd`: на каждом control-plane узле работает локальный экземпляр etcd.

## 3.2. Kubernetes API endpoint

Все control-plane узлы должны использовать единый endpoint:

```text
k8s-api.<environment>.<domain>:6443
```

Endpoint указывает на VIP, обслуживаемый одним из вариантов:

* HAProxy + Keepalived на выделенной паре ВМ (устанавливается этим playbook, этап 05);
* существующий корпоративный Load Balancer (тогда этап 05 пропускается, а в preflight проверяется доступность VIP).

Выбранный вариант и адрес VIP фиксируются в переменных inventory (`api_vip`). Нельзя указывать IP первого control-plane узла как постоянный `controlPlaneEndpoint`.

## 3.3. Стратегия taints и labels

Фиксируется до установки, так как является входными данными для GitOps-слоя (nodeSelector/tolerations platform-компонентов):

* control-plane узлы сохраняют стандартный taint `node-role.kubernetes.io/control-plane:NoSchedule`, выставляемый kubeadm; пользовательские workload на них не размещаются;
* worker-узлы получают label `node-role.kubernetes.io/worker=""` — **отдельной задачей после join**, от имени cluster-admin, поскольку kubelet не вправе выставлять labels в домене `kubernetes.io` (раздел 16.1);
* выделенные пулы (infra, ingress, storage) получают бездоменный label `node-pool=<name>` при регистрации через kubeletExtraArgs `--node-labels`;
* **taints выделенных пулов выставляются при регистрации узла** через `nodeRegistration.taints` в JoinConfiguration, а не через GitOps. Назначение taints декларативно означало бы, что Argo CD владеет объектами `Node`: они cluster-scoped, могут не существовать на момент sync, а их статус постоянно переписывает kubelet. Кроме того, между появлением узла в состоянии `Ready` и применением taint остаётся окно, в которое успевает приземлиться посторонний workload;
* platform-компоненты (observability, ingress) в GitOps-манифестах используют nodeSelector по `node-pool`, если пулы выделены.

---

# 4. Граница GitOps

## 4.1. Что нельзя первоначально установить через Argo CD

Argo CD не может развернуть сам себя в полностью пустом кластере. До появления CNI кластер не способен запускать обычные Pod.

Bootstrap-цепочка:

```text
Virtual Machines
        ↓
Ansible: Load Balancer (или проверка внешнего VIP)
        ↓
Ansible: OS preparation
        ↓
Ansible: containerd + kubeadm
        ↓
kubeadm init
        ↓
Ansible: CNI bootstrap (helm template, values из GitOps-репозитория)
        ↓
Ansible: Argo CD installation
        ↓
Ansible: root Application
        ↓
Argo CD: cluster reconciliation (включая takeover Cilium)
```

## 4.2. Bootstrap-граница

Через Ansible устанавливаются только:

1. Load Balancer для API endpoint (если не внешний);
2. Kubernetes control plane;
3. Cilium (bootstrap-установка);
4. namespace `argocd`;
5. первоначальная версия Argo CD;
6. Secret доступа к Git;
7. корневой объект `Application`.

Всё остальное устанавливается через Argo CD.

## 4.3. Управление CNI: bootstrap и takeover

Целевая модель: Ansible выполняет только первоначальную установку Cilium, дальнейшее управление — Argo CD.

Ключевые требования, устраняющие drift и конфликт владения:

1. **Единый источник values.** Ansible на этапе 50 выполняет checkout файла `clusters/<cluster>/values/cilium.yaml` из GitOps-репозитория и использует его без модификаций. Ansible не имеет собственного шаблона values для Cilium.

2. **Установка через `helm template`, а не `helm install`.** В кластере не создаётся helm release secret (`sh.helm.release.v1.*`), поэтому после takeover не остаётся осиротевшего релиза.

Формы указания чарта **взаимоисключающие**, смешивать их нельзя:

```bash
# OCI-реестр (Harbor) — рекомендуемый вариант для закрытого контура
helm template cilium oci://harbor.company.local/charts/cilium \
  --version "${CILIUM_VERSION}" \
  --namespace kube-system \
  --values /tmp/gitops-checkout/clusters/prod-01/values/cilium.yaml \
  | kubectl apply --server-side --field-manager=argocd-controller -f -

# Классический Helm-репозиторий: имя чарта БЕЗ префикса репозитория
helm template cilium cilium \
  --repo "${CILIUM_HELM_REPO}" \
  --version "${CILIUM_VERSION}" \
  --namespace kube-system \
  --values /tmp/gitops-checkout/clusters/prod-01/values/cilium.yaml \
  | kubectl apply --server-side --field-manager=argocd-controller -f -
```

Запись `helm template cilium cilium/cilium --repo <url>` ошибочна: с `--repo` helm ищет чарт с именем `cilium/cilium` внутри указанного репозитория и не находит его.

`--field-manager=argocd-controller` задаётся намеренно: bootstrap применяет манифесты тем же field manager, которым затем работает Argo CD, и takeover через Server-Side Apply проходит без конфликтов владения полями. Имя менеджера следует сверить с целевой версией Argo CD.

3. **Takeover через Argo CD Application** (wave -10) с той же версией чарта и тем же values-файлом. Server-Side Apply корректно перехватывает field ownership:

```yaml
syncPolicy:
  syncOptions:
    - ServerSideApply=true
    - RespectIgnoreDifferences=true
```

4. **Порядок включения автоматики:**
   * первый sync — вручную, с предварительным `argocd app diff`;
   * ожидаемые расхождения: tracking-labels, поля контроллеров (status, caBundle); реальные расхождения в spec — стоп-сигнал, исправляются в values;
   * генерируемые чартом сертификаты (`cilium-ca`, hubble TLS) выносятся в фиксированные Secret или закрываются `ignoreDifferences`, иначе Application будет вечно `OutOfSync`;
   * после успешного sync и `cilium status --wait` включается `automated` + `selfHeal`;
   * `prune: true` для Cilium включается последним, осознанно; на DaemonSet `cilium` и ConfigMap `cilium-config` ставится аннотация `argocd.argoproj.io/sync-options: Prune=false`.

Обновление версии Cilium после takeover выполняется только через PR в GitOps-репозиторий.

---

# 5. Требования к виртуальным машинам

## 5.1. Операционная система

Все узлы одного кластера должны использовать одинаковую:

* операционную систему;
* major-версию ядра;
* версию containerd;
* версию kubelet;
* схему сетевых интерфейсов;
* схему дисков.

Рекомендуется минимальный серверный образ без лишних сервисов, например `Ubuntu Server 24.04 LTS`, либо корпоративный дистрибутив. Узлы создаются из golden image (Packer/qemu + zVirt), обновление пулов — blue/green.

## 5.2. Минимальные ресурсы

### Load balancer

```text
CPU:     2 vCPU
RAM:     4 GB
Disk OS: 40 GB
```

### Control plane

```text
CPU:     4 vCPU
RAM:     8–16 GB
Disk OS: 80 GB
Disk etcd: отдельный диск (обязателен для prod)
```

### Worker

```text
CPU:     от 8 vCPU
RAM:     от 16 GB
Disk OS: от 100 GB
```

## 5.3. Сетевые требования

Каждый узел должен иметь:

* постоянный IP-адрес;
* корректный hostname;
* прямую связность со всеми узлами кластера;
* доступ к внутреннему DNS;
* синхронизацию времени;
* доступ к container registry (Harbor);
* доступ к Git;
* доступ к репозиториям пакетов или внутреннему mirror;
* доступ к PKI, OpenBao и observability-системам.

Пара load balancer дополнительно: VRRP (protocol 112) между собой, доступ на 6443 всех control-plane узлов.

## 5.4. DNS-записи

Перед установкой должны быть созданы:

```text
k8s-api.<cluster-domain>   → api_vip
argocd.<cluster-domain>
registry.<domain>
harbor.<domain>
```

---

# 6. Предварительные решения

Перед запуском playbook должны быть определены:

```yaml
cluster_name: prod-01

# НЕ `environment`: это зарезервированное ключевое слово Ansible на уровне
# play и task, переменная с таким именем даёт трудноуловимые конфликты.
cluster_environment: prod

kubernetes_version: "1.xx.y"
containerd_version: "2.x.y"

api_vip: "10.10.1.20"
api_vip_managed: true          # false — внешний корпоративный LB
control_plane_endpoint: "k8s-api.prod.company.local:6443"

pod_network_cidr: "10.100.0.0/16"
service_network_cidr: "10.101.0.0/16"
cluster_dns: "10.101.0.10"
cluster_domain: "cluster.local"

cni:
  name: cilium
  version: "x.y.z"
  helm_repo: "https://helm.cilium.io/"   # или внутреннее зеркало в Harbor

argocd:
  version: "x.y.z"
  repository: "ssh://git@gitlab.company.local/platform/kubernetes-gitops.git"
  revision: main
  path: "clusters/prod-01"

etcd_encryption_provider: secretbox   # secretbox | aescbc | kms
audit_log_max_age: 30
audit_log_max_backup: 10
audit_log_max_size: 100

pod_security_admission_enforce: baseline
tls_min_version: VersionTLS12
tls_cipher_suites:
  - TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
  - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
  - TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
  - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
```

Версии компонентов должны быть явно закреплены. Использование `latest` запрещается. Helm-чарты и образы зеркалируются во внутренний Harbor; прямой доступ узлов в интернет не предполагается.

---

# 7. Структура Ansible-проекта

```text
kubernetes-install/
├── ansible.cfg
├── requirements.yml
├── requirements-test.txt
├── inventories/
│   ├── prod-01/
│   │   ├── hosts.yml
│   │   ├── group_vars/
│   │   │   ├── all.yml
│   │   │   ├── load_balancers.yml
│   │   │   ├── control_plane.yml
│   │   │   └── workers.yml
│   │   └── host_vars/
│   └── stage-01/
├── playbooks/
│   ├── 00-preflight.yml
│   ├── 05-load-balancer.yml
│   ├── 10-os-prepare.yml
│   ├── 20-containerd.yml
│   ├── 30-kubernetes-packages.yml
│   ├── 40-control-plane-init.yml
│   ├── 41-control-plane-join.yml
│   ├── 42-workers-join.yml
│   ├── 50-cni-bootstrap.yml
│   ├── 60-argocd-bootstrap.yml
│   ├── 70-validation.yml
│   ├── 80-backup.yml
│   └── site.yml
├── roles/
│   ├── preflight/
│   ├── haproxy_keepalived/
│   ├── os_prepare/
│   ├── kernel/
│   ├── containerd/
│   ├── kubernetes_packages/
│   ├── kubeadm_config/
│   ├── control_plane_init/
│   ├── node_join/
│   ├── cilium_bootstrap/
│   ├── argocd_bootstrap/
│   ├── etcd_backup/
│   └── cluster_validation/
│   └── resources/           # create/destroy ВМ для delegated-драйвера
│   └── site-full/
└── Makefile
```

Шаблоны лежат в `roles/<role>/templates/`, а не в общем каталоге `templates/`: с общим каталогом роль перестаёт быть самодостаточной и её нельзя применить отдельно от остального проекта.

Соответствие шаблонов ролям:

| Шаблон | Роль |
| ------ | ---- |
| `haproxy.cfg.j2`, `keepalived.conf.j2`, `check_haproxy.sh.j2` | `haproxy_keepalived` |
| `modules-load.conf.j2`, `sysctl.conf.j2` | `kernel` |
| `chrony.conf.j2`, `journald-kubernetes.conf.j2`, `logrotate-containers.j2` | `os_prepare` |
| `config.toml.j2`, `hosts.toml.j2`, `crictl.yaml.j2`, `containerd.service.j2` | `containerd` |
| `kubeadm-init.yaml.j2`, `encryption-config.yaml.j2`, `audit-policy.yaml.j2`, `admission-config.yaml.j2` | `kubeadm_config` |
| `kubeadm-join-control-plane.yaml.j2`, `kubeadm-join-worker.yaml.j2` | `node_join` |
| `etcd-snapshot.sh.j2`, `etcd-backup.{service,timer}.j2`, `etcd-backup-cronjob.yaml.j2` | `etcd_backup` |

---

# 8. Inventory

Пример `inventories/prod-01/hosts.yml`:

```yaml
all:
  vars:
    ansible_user: ansible
    ansible_become: true

  children:
    load_balancers:
      hosts:
        k8s-lb-01:
          ansible_host: 10.10.1.10
          keepalived_priority: 150
        k8s-lb-02:
          ansible_host: 10.10.1.11
          keepalived_priority: 100

    control_plane:
      hosts:
        k8s-cp-01:
          ansible_host: 10.10.1.21
        k8s-cp-02:
          ansible_host: 10.10.1.22
        k8s-cp-03:
          ansible_host: 10.10.1.23

    workers:
      hosts:
        k8s-worker-01:
          ansible_host: 10.10.1.31
        k8s-worker-02:
          ansible_host: 10.10.1.32
        k8s-worker-03:
          ansible_host: 10.10.1.33

    kubernetes:
      children:
        control_plane:
        workers:
```

---

# 9. Этап 00. Preflight-проверки

Роль `preflight` выполняет проверки до изменения серверов.

## 9.1. Проверка Ansible

* доступ по SSH;
* privilege escalation;
* корректность inventory;
* отсутствие дублирующихся IP;
* уникальность hostname;
* доступность всех узлов.

## 9.2. Проверка ресурсов

* количество CPU;
* объём RAM;
* свободное место;
* наличие отдельного диска etcd на control-plane;
* поддерживаемая версия ядра;
* отсутствие read-only filesystem.

## 9.3. Проверка сети

* резолвинг hostname и `k8s-api.<domain>` → `api_vip`;
* при `api_vip_managed: false` — доступность внешнего VIP;
* связность между узлами;
* доступ к registry (Harbor) и Git;
* отсутствие пересечения Pod CIDR и Service CIDR с корпоративными сетями;
* корректность MTU;
* открытие необходимых портов:
  * 6443 — kube-apiserver;
  * 2379-2380 — etcd client и peer;
  * 10250 — kubelet API;
  * 30000-32767 — диапазон NodePort;
  * VRRP (protocol 112) между узлами LB;
  * Cilium: 4240 (health), 4244 (hubble server), 4245 (hubble relay), 8472/UDP (VXLAN, если не native routing), ICMP для health-проверок.

## 9.4. Проверка времени

* работа chrony или systemd-timesyncd;
* допустимое расхождение времени;
* доступность NTP-серверов.

## 9.5. Fail-fast

Playbook завершается до установки Kubernetes, если хотя бы одна обязательная проверка не пройдена.

---

# 10. Этап 05. Load Balancer

Выполняется только при `api_vip_managed: true`. Роль `haproxy_keepalived`:

1. устанавливает закреплённые версии haproxy и keepalived;
2. настраивает HAProxy: frontend `:6443` (mode tcp) → backend все control-plane узлы, health check `/healthz` через `check-ssl verify none` либо tcp-check;
3. настраивает Keepalived: VRRP instance с `api_vip`, приоритеты из inventory, unicast peers, track script на процесс haproxy;
4. включает нестандартный `net.ipv4.ip_nonlocal_bind` при необходимости;
5. проверяет: VIP поднят на master-узле, failover при остановке haproxy на master.

Пример backend HAProxy:

```text
backend k8s-api
    mode tcp
    balance roundrobin
    option tcp-check

    # Проверка готовности по HTTP, а не голым TCP: apiserver может слушать
    # порт, но возвращать 500 на /readyz — например, во время восстановления
    # etcd или пока не прогрелись кеши. Голый `check` в этот момент отдаёт
    # ему клиентский трафик.
    option httpchk GET /readyz
    http-check expect status 200

    server k8s-cp-01 10.10.1.21:6443 check check-ssl verify none inter 3s fall 2 rise 3
    server k8s-cp-02 10.10.1.22:6443 check check-ssl verify none inter 3s fall 2 rise 3
    server k8s-cp-03 10.10.1.23:6443 check check-ssl verify none inter 3s fall 2 rise 3
```

`/readyz`, а не `/healthz`: последний для целей балансировки считается устаревшим и не отражает готовность принимать трафик.

Таймауты `client` и `server` выставляются в часы: watch-запросы к apiserver держат соединение долго, и короткий таймаут заставляет контроллеры непрерывно переподключаться.

До `kubeadm init` backend-узлы недоступны — это ожидаемо; HAProxy должен стартовать при отсутствии живых backend. По этой же причине track script keepalived проверяет наличие процесса haproxy, а не ответ через сам балансировщик: иначе VIP не поднимется и установка не начнётся.

При использовании внешнего корпоративного LB этот этап пропускается, а конфигурация LB (frontend, backend-пул, health check) фиксируется в документации кластера и резервируется (см. раздел 28).

---

# 11. Этап 10. Подготовка операционной системы

Роль `os_prepare` выполняет:

1. настройку hostname;
2. настройку DNS;
3. настройку `/etc/hosts`, если требуется;
4. установку базовых пакетов;
5. настройку NTP;
6. отключение swap;
7. загрузку kernel modules;
8. настройку sysctl;
9. настройку firewall;
10. настройку proxy и registry mirror;
11. настройку log rotation;
12. установку корневых CA организации.

Kernel modules:

```text
overlay
br_netfilter
```

Sysctl. Первая группа — сетевые требования Kubernetes, вторая — **обязательные значения для `protectKernelDefaults: true`**.

При `protectKernelDefaults: true` kubelet не выставляет sysctl, а **падает при несовпадении**. Проверяется ровно шесть значений — перечислять меньше нельзя:

```yaml
# Kubernetes networking
net.bridge.bridge-nf-call-iptables: 1
net.bridge.bridge-nf-call-ip6tables: 1
net.ipv4.ip_forward: 1

# Требования protectKernelDefaults — полный набор
vm.overcommit_memory: 1
vm.panic_on_oom: 0
kernel.panic: 10
kernel.panic_on_oops: 1
kernel.keys.root_maxkeys: 1000000     # дефолт Linux 200
kernel.keys.root_maxbytes: 25000000   # дефолт Linux 20000
```

Два последних отличаются от дефолтов ядра. Без них kubelet не стартует **ни на одном узле**:

```text
Invalid kernel flag: kernel/keys/root_maxkeys, expected value 1000000, actual value 200
```

Проверяются фактические значения в `/proc/sys`, а не факт записи в `sysctl.d`: значение может быть перебито более приоритетным файлом, настройкой облачного агента или cloud-init.

Отдельно поднимаются лимиты inotify — дефолтов не хватает узлу с сотнями Pod, каждый смонтированный том держит watch:

```yaml
fs.inotify.max_user_instances: 8192
fs.inotify.max_user_watches: 524288
```

Swap отключается и в текущей системе, и в `/etc/fstab`, а также маскируются systemd-юниты типа `swap` и генератор zram — иначе swap вернётся после перезагрузки:

```bash
swapoff -a
```

**Отдельный диск etcd** монтируется в `/var/lib/etcd` на этом этапе, **до** `kubeadm init`. kubeadm создаёт каталог при инициализации; монтирование диска поверх уже наполненного каталога спрячет данные, и кластер поднимется с пустой базой.

---

# 12. Этап 20. Установка containerd

Роль `containerd` (версия 2.x):

1. устанавливает закреплённую версию containerd;
2. создаёт `/etc/containerd/config.toml`;
3. включает CRI plugin, `SystemdCgroup = true`;
4. настраивает registry mirrors **через `config_path` и hosts.toml** (формат mirrors внутри config.toml в containerd 2.x удалён);
5. выравнивает `sandbox_image` с версией pause, ожидаемой целевой версией kubeadm (`kubeadm config images list`);
6. устанавливает корпоративные CA;
7. создаёт `/etc/crictl.yaml`;
8. включает и запускает service;
9. проверяет CRI endpoint.

`config.toml`:

```toml
version = 3

[plugins."io.containerd.cri.v1.images".pinned_images]
  sandbox = "registry.company.local/pause:3.x"

[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runc.options]
  SystemdCgroup = true

[plugins."io.containerd.cri.v1.images".registry]
  config_path = "/etc/containerd/certs.d"
```

Пример `/etc/containerd/certs.d/docker.io/hosts.toml`:

```toml
server = "https://docker.io"

[host."https://harbor.company.local/v2/proxy-docker.io"]
  capabilities = ["pull", "resolve"]
  override_path = true
```

`/etc/crictl.yaml`:

```yaml
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
```

Проверка:

```bash
systemctl is-active containerd
crictl info
crictl pull registry.company.local/pause:3.x
```

Для kubelet и container runtime используется один cgroup driver (systemd). Шаблон config.toml обязан соответствовать закреплённой major-версии containerd; при обновлении containerd 1.x → 2.x шаблон пересматривается.

---

# 13. Этап 30. Установка Kubernetes packages

Роль `kubernetes_packages` устанавливает:

```text
kubelet
kubeadm
kubectl
cri-tools
```

Требования:

* версия Kubernetes закреплена;
* пакеты защищены от автоматического обновления (apt-mark hold / versionlock);
* kubelet включён в systemd;
* repository key и URL задаются переменными (внутренний mirror);
* запрещено смешивать разные minor-версии kubelet и kubeadm вне контролируемого upgrade.

Проверка:

```bash
kubeadm version
kubelet --version
kubectl version --client
crictl version
```

---

# 14. Этап 40. Инициализация первого control-plane узла

## 14.1. Подготовка конфигураций безопасности

До `kubeadm init` Ansible размещает на всех control-plane узлах:

**`/etc/kubernetes/encryption-config.yaml`** (mode 0600, владелец root) — шифрование Secrets в etcd:

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  - resources:
      - secrets
    providers:
      - secretbox:
          keys:
            - name: key1
              secret: "{{ etcd_encryption_key }}"
      - identity: {}
```

**Выбор провайдера.** Дефолт — `secretbox` (XSalsa20-Poly1305). Провайдер `aescbc` в апстрим-документации помечен как нерекомендуемый: CBC уязвим к padding oracle. Он остаётся поддерживаемым для существующих кластеров, но для новой установки выбирать его не следует. При наличии KMS-плагина для OpenBao предпочтителен provider `kms v2` — тогда ключ вообще не покидает OpenBao.

`identity` в конце списка обязателен: без него apiserver не прочитает Secrets, записанные до включения шифрования.

Ключ генерируется один раз (`head -c 32 /dev/urandom | base64`, ровно 32 байта), хранится в OpenBao, доставляется Ansible с `no_log: true`, не попадает в Git и логи.

Полезно добавить флаг `--encryption-provider-config-automatic-reload=true`: apiserver перечитывает конфигурацию без рестарта, что упрощает ротацию.

### Ротация ключа шифрования

Порядок провайдеров значим: apiserver **пишет** первым провайдером, а **читает** любым из перечисленных. Поэтому в HA-кластере ротация выполняется в **четыре фазы**, а не в три — иначе, пока не все apiserver знают новый ключ, часть запросов к Secrets будет падать.

| Фаза | Действие | Состояние |
| ---- | -------- | --------- |
| 1 | Новый ключ добавляется **вторым** провайдером на всех узлах, рестарт всех apiserver | Пишем старым, читать умеем оба |
| 2 | Новый ключ переставляется **первым** на всех узлах, рестарт всех apiserver | Пишем новым, старый ещё читается |
| 3 | Перешифровка: `kubectl get secrets -A -o json \| kubectl replace -f -` | Все объекты переписаны новым ключом |
| 4 | Старый ключ удаляется, рестарт всех apiserver | Остался один ключ |

Между фазами выполняется проверка: `kubectl get secrets -A` должен отрабатывать без ошибок на каждом узле.

**Снимок etcd, снятый между фазами 1 и 4, требует для восстановления оба ключа.** Это учитывается в регламенте резервного копирования (раздел 28.2).

**`/etc/kubernetes/audit-policy.yaml`** — политика аудита apiserver. Минимальный вариант:

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages:
  - RequestReceived
rules:
  - level: None
    users: ["system:kube-proxy"]
    verbs: ["watch"]
  - level: None
    userGroups: ["system:nodes"]
    verbs: ["get"]
    resources:
      - group: ""
        resources: ["nodes", "nodes/status"]
  - level: Metadata
    resources:
      - group: ""
        resources: ["secrets", "configmaps"]
  - level: RequestResponse
    verbs: ["create", "update", "patch", "delete"]
    resources:
      - group: ""
      - group: "rbac.authorization.k8s.io"
      - group: "apps"
  - level: Metadata
```

Финальная политика согласуется с ИБ. Логи аудита отгружаются observability-агентом (Wave 0) в центральное хранилище.

## 14.2. kubeadm configuration

```yaml
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: 10.10.1.21
  bindPort: 6443
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
  kubeletExtraArgs:
    - name: node-ip
      value: 10.10.1.21
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
clusterName: prod-01
kubernetesVersion: v1.xx.y
controlPlaneEndpoint: k8s-api.prod.company.local:6443
imageRepository: registry.company.local/k8s
networking:
  podSubnet: 10.100.0.0/16
  serviceSubnet: 10.101.0.0/16
  dnsDomain: cluster.local
apiServer:
  certSANs:
    - k8s-api.prod.company.local
    - "{{ api_vip }}"
  extraArgs:
    - name: encryption-provider-config
      value: /etc/kubernetes/encryption-config.yaml
    - name: encryption-provider-config-automatic-reload
      value: "true"
    # Pod Security Admission: закрывает окно между kubeadm init и
    # установкой policy engine в wave 0
    - name: admission-control-config-file
      value: /etc/kubernetes/admission-config.yaml
    - name: tls-min-version
      value: "{{ tls_min_version }}"
    - name: tls-cipher-suites
      value: "{{ tls_cipher_suites | join(',') }}"
    - name: profiling
      value: "false"
    - name: audit-policy-file
      value: /etc/kubernetes/audit-policy.yaml
    - name: audit-log-path
      value: /var/log/kubernetes/audit/audit.log
    - name: audit-log-maxage
      value: "{{ audit_log_max_age }}"
    - name: audit-log-maxbackup
      value: "{{ audit_log_max_backup }}"
    - name: audit-log-maxsize
      value: "{{ audit_log_max_size }}"
  extraVolumes:
    - name: encryption-config
      hostPath: /etc/kubernetes/encryption-config.yaml
      mountPath: /etc/kubernetes/encryption-config.yaml
      readOnly: true
      pathType: File
    - name: audit-policy
      hostPath: /etc/kubernetes/audit-policy.yaml
      mountPath: /etc/kubernetes/audit-policy.yaml
      readOnly: true
      pathType: File
    - name: audit-log
      hostPath: /var/log/kubernetes/audit
      mountPath: /var/log/kubernetes/audit
      pathType: DirectoryOrCreate
    - name: admission-config
      hostPath: /etc/kubernetes/admission-config.yaml
      mountPath: /etc/kubernetes/admission-config.yaml
      readOnly: true
      pathType: File
controllerManager:
  extraArgs:
    - name: tls-min-version
      value: "{{ tls_min_version }}"
    - name: tls-cipher-suites
      value: "{{ tls_cipher_suites | join(',') }}"
    - name: profiling
      value: "false"
scheduler:
  extraArgs:
    - name: tls-min-version
      value: "{{ tls_min_version }}"
    - name: tls-cipher-suites
      value: "{{ tls_cipher_suites | join(',') }}"
    - name: profiling
      value: "false"
etcd:
  local:
    dataDir: /var/lib/etcd
    extraArgs:
      # etcd принимает версию TLS в формате TLS1.2, apiserver — VersionTLS12
      - name: tls-min-version
        value: TLS1.2
      - name: cipher-suites
        value: "{{ tls_cipher_suites | join(',') }}"
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
protectKernelDefaults: true
rotateCertificates: true
serverTLSBootstrap: true
tlsMinVersion: "{{ tls_min_version }}"
tlsCipherSuites: "{{ tls_cipher_suites }}"
containerLogMaxSize: "50Mi"
containerLogMaxFiles: 5
systemReserved:
  cpu: "500m"
  memory: "1Gi"
  ephemeral-storage: "5Gi"
kubeReserved:
  cpu: "500m"
  memory: "1Gi"
  ephemeral-storage: "5Gi"
evictionHard:
  memory.available: "500Mi"
  nodefs.available: "10%"
  nodefs.inodesFree: "5%"
  imagefs.available: "15%"
```

`evictionHard` **заменяет карту дефолтов целиком**, а не дополняет её. Поэтому `nodefs.inodesFree` присутствует явно: без него исчерпание inode перестанет вытеснять Pod'ы, и узел встанет с полным диском при формально свободном месте.

Значения reserved/eviction — стартовые; уточняются по фактическому потреблению системных компонентов на целевых размерах узлов.

### Pod Security Admission

Файл `/etc/kubernetes/admission-config.yaml`, подключаемый через `admission-control-config-file`. Между `kubeadm init` и установкой policy engine (wave 0) кластер иначе ничем не защищён, а встроенный PSA не требует установки компонентов:

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: AdmissionConfiguration
plugins:
  - name: PodSecurity
    configuration:
      apiVersion: pod-security.admission.config.k8s.io/v1
      kind: PodSecurityConfiguration
      defaults:
        enforce: baseline
        enforce-version: latest
        audit: restricted
        audit-version: latest
        warn: restricted
        warn-version: latest
      exemptions:
        usernames: []
        runtimeClasses: []
        namespaces:
          - kube-system
```

`kube-system` выведен в исключения: static pods control plane и системные DaemonSet не проходят baseline.

## 14.3. Инициализация

```bash
kubeadm init --config /etc/kubernetes/kubeadm-init.yaml --upload-certs
```

После выполнения:

* kubeconfig копируется в защищённое расположение;
* join-команды не пишутся в открытые логи;
* certificate key не сохраняется в Git;
* bootstrap token имеет ограниченный TTL;
* результат регистрируется Ansible.

## 14.4. Kubelet serving CSR

`serverTLSBootstrap: true` означает, что kubelet каждого узла запрашивает serving-сертификат через CSR API, и **kubeadm эти CSR не аппрувит автоматически**. Без аппрува CSR остаются в Pending, а metrics-server не может обращаться к kubelet по TLS.

Решение — двухступенчатое:

1. **Bootstrap**: роль `cluster_validation` (этап 70) аппрувит pending kubelet serving CSR один раз, после присоединения всех узлов:

```bash
kubectl get csr -o json \
  | jq -r '.items[] | select(.spec.signerName=="kubernetes.io/kubelet-serving") | select(.status=={}) | .metadata.name' \
  | xargs -r -n1 kubectl certificate approve
```

2. **Постоянно**: через Argo CD (wave -10) устанавливается `kubelet-csr-approver` (postfinance) с валидацией по списку допустимых node names/IP из inventory. Он обслуживает ротацию serving-сертификатов в течение жизни кластера.

---

# 15. Этап 41. Присоединение control-plane узлов

Перед присоединением проверить:

```bash
curl -k https://k8s-api.prod.company.local:6443/livez
```

Также убедиться, что encryption-config и audit-policy размещены на присоединяемом узле (kubeadm join поднимет apiserver с теми же extraArgs из ClusterConfiguration).

Join configuration:

```yaml
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
discovery:
  bootstrapToken:
    apiServerEndpoint: k8s-api.prod.company.local:6443
    token: "{{ kubeadm_token }}"
    caCertHashes:
      - "{{ discovery_token_ca_cert_hash }}"
controlPlane:
  certificateKey: "{{ certificate_key }}"
  localAPIEndpoint:
    advertiseAddress: "{{ ansible_host }}"
    bindPort: 6443
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
  kubeletExtraArgs:
    - name: node-ip
      value: "{{ ansible_host }}"
```

Присоединение выполняется последовательно, по одному узлу. После каждого:

```bash
kubectl get nodes
kubectl get pods -n kube-system
kubectl get --raw='/readyz?verbose'
```

---

# 16. Этап 42. Присоединение worker-узлов

Worker join configuration:

```yaml
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
discovery:
  bootstrapToken:
    apiServerEndpoint: k8s-api.prod.company.local:6443
    token: "{{ kubeadm_token }}"
    caCertHashes:
      - "{{ discovery_token_ca_cert_hash }}"
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
  # Taints выделенных пулов выставляются здесь, при регистрации (раздел 3.3)
  taints: []
  kubeletExtraArgs:
    - name: node-ip
      value: "{{ ansible_host }}"
    # ТОЛЬКО бездоменные labels — см. ниже
    - name: node-labels
      value: "node-pool={{ node_pool | default('general') }}"
```

## 16.1. Назначение роли узла

**Kubelet не может назначить себе label в домене `kubernetes.io`.** Admission-плагин `NodeRestriction`, включаемый kubeadm по умолчанию, пропускает от kubelet лишь короткий whitelist. Передача `node-role.kubernetes.io/worker=` через `--node-labels` приводит к отказу регистрации:

```text
is forbidden: node "k8s-worker-01" is not allowed to modify labels:
node-role.kubernetes.io/worker
```

Бездоменный `node-pool` проходит — ограничение касается только доменов `kubernetes.io` и `k8s.io`.

Роль узла назначается отдельной задачей после join, от имени cluster-admin:

```yaml
- name: Assign worker role label
  kubernetes.core.k8s:
    kubeconfig: "{{ kubeconfig_path }}"
    state: patched
    kind: Node
    name: "{{ inventory_hostname }}"
    definition:
      metadata:
        labels:
          node-role.kubernetes.io/worker: ""
  delegate_to: "{{ groups['control_plane'] | first }}"
```

Задача выполняется при каждом прогоне, а не только после join: label мог быть снят вручную, а platform-компоненты в GitOps используют по нему nodeSelector.

Присоединение может выполняться параллельно с ограниченным `serial: 2`.

Worker-узлы остаются `NotReady` до установки CNI — это ожидаемое состояние.

---

# 17. Этап 50. Bootstrap Cilium

Порядок (роль `cilium_bootstrap`):

1. checkout GitOps-репозитория (read-only, `clusters/<cluster>/values/`);
2. рендер и применение чарта — **без создания helm release**:

```bash
helm template cilium oci://harbor.company.local/charts/cilium \
  --version "{{ cni.version }}" \
  --namespace kube-system \
  --values /tmp/gitops-checkout/clusters/prod-01/values/cilium.yaml \
  | kubectl apply --server-side --force-conflicts \
      --field-manager=argocd-controller -f -
```

Форма вызова — одна из двух, описанных в разделе 4.3; смешивать их нельзя. Реализация собирает команду в одном месте, чтобы формы не разъехались между ролями.

Чарт берётся из внутреннего зеркала в Harbor (или официального `https://helm.cilium.io/`, если контур это допускает). Доступность источника проверяется в preflight.

После применения проверяется отсутствие helm release secret — это контроль того, что установка действительно шла через `helm template`:

```bash
kubectl -n kube-system get secret -l owner=helm,name=cilium
# ожидается: No resources found
```

Минимальные параметры в `values/cilium.yaml`:

```yaml
kubeProxyReplacement: false

ipam:
  mode: kubernetes

operator:
  replicas: 2

hubble:
  enabled: true
  relay:
    enabled: true
  ui:
    enabled: false

rollOutCiliumPods: true
```

На первом этапе сохраняется стандартный kube-proxy. Его замена Cilium — отдельный ADR с отдельным тестированием.

Проверка после установки:

```bash
cilium status --wait
kubectl get nodes
kubectl get pods -A
```

**Connectivity test в закрытом контуре.** Часть тестов `cilium connectivity test` обращается к внешним ресурсам (1.1.1.1, публичный DNS) и в изолированной сети гарантированно падает. Фиксируется профиль запуска:

```bash
cilium connectivity test \
  --test '!to-fqdns' \
  --test '!client-egress-l7' \
  --external-target harbor.company.local \
  --external-cidr 10.0.0.0/8 \
  --external-ip <внутренний IP> \
  --external-other-ip <внутренний IP-2>
```

Конкретный набор исключений подбирается один раз при внедрении и фиксируется в переменных роли. После теста namespace `cilium-test-*` обязательно удаляется. Все узлы должны перейти в `Ready`.

---

# 18. Этап 60. Bootstrap Argo CD

## 18.1. Установка

Ansible создаёт namespace `argocd` и устанавливает Argo CD:

* из официального Helm chart (через зеркало в Harbor);
* с закреплённой версией;
* с минимальным bootstrap values-файлом, взятым из GitOps-репозитория (`clusters/<cluster>/values/argocd.yaml`) — тот же принцип единого источника values, что и для Cilium;
* установка также через `helm template | kubectl apply --server-side`, чтобы takeover самим Argo CD (wave -20) прошёл без осиротевшего helm release.

## 18.2. Доступ к Git

Предпочтительный вариант: deploy key (read-only) на GitOps-репозиторий.

Git credentials не должны храниться:

* в inventory;
* в plaintext group_vars;
* в Git-репозитории;
* в логах CI;
* в сохранённом Ansible output.

Для первой установки Secret создаётся Ansible из OpenBao (lookup plugin, `no_log: true`). После запуска External Secrets Operator управление credentials передаётся ему: в Git хранится `ExternalSecret`, ссылающийся на OpenBao.

## 18.3. Root Application

Манифест root Application **хранится в GitOps-репозитории** (`clusters/<cluster>/bootstrap/root-application.yaml`) и входит в `bootstrap/kustomization.yaml`. Ansible применяет его без модификаций и собственного шаблона не имеет.

Это принципиально. Если манифест существует только в Ansible, приложение не самоуправляемо: перевод его в AppProject `platform-root` (wave -20) и любые последующие изменения останутся ручной операцией, а Git перестанет быть источником целевого состояния.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: cluster-bootstrap
  namespace: argocd
  # finalizers НЕ задаются — см. ниже
spec:
  project: default          # wave -20 переводит в platform-root

  source:
    repoURL: ssh://git@gitlab.company.local/platform/kubernetes-gitops.git
    targetRevision: main
    path: clusters/prod-01/bootstrap

  destination:
    server: https://kubernetes.default.svc
    namespace: argocd

  syncPolicy:
    # syncPolicy.automated при первом bootstrap ОТСУТСТВУЕТ — см. ниже
    syncOptions:
      - CreateNamespace=true
```

**Защита от каскадного удаления.** Finalizer `resources-finalizer.argocd.argoproj.io` на root Application **не устанавливается**: с ним случайное удаление `cluster-bootstrap` каскадно удалит всю кластерную конфигурацию. Без finalizer удаление осиротит дочерние объекты, но не удалит их; восстановление — повторное применение манифеста.

**Автоматика при первом bootstrap не включается.** Блок `syncPolicy.automated` в манифесте отсутствует. Ошибка в путях ApplicationSet при включённом `prune` приведёт к массовому удалению ресурсов ещё до того, как кто-либо посмотрит diff. Порядок такой же, как для Cilium (раздел 4.3):

1. `argocd app diff cluster-bootstrap` — разобрать все расхождения;
2. `argocd app sync cluster-bootstrap` — первый sync вручную;
3. убедиться, что дочерние Applications созданы и Cilium `Synced` без диффов в spec;
4. включить `automated` + `selfHeal` отдельным PR;
5. `prune: true` — последним, осознанно.

Ansible проверяет оба условия и отказывается применять манифест, если в нём есть finalizer или `automated` при выключенном `argocd.root_app_automated`.

Дополнительно (wave -20):

* root Application переводится из `default` в выделенный AppProject `platform-root`;
* в проекте запрещается delete через Argo CD RBAC;
* дочерние ApplicationSets получают `preservedFields` / отключённый cascade там, где это критично.

После применения root Application и первого sync дальнейшую установку выполняет Argo CD.

---

# 19. Структура GitOps-репозитория

```text
kubernetes-gitops/
├── README.md
├── clusters/
│   ├── prod-01/
│   │   ├── bootstrap/
│   │   │   ├── kustomization.yaml
│   │   │   ├── projects.yaml
│   │   │   ├── infrastructure-appset.yaml
│   │   │   └── platform-appset.yaml
│   │   ├── values/
│   │   │   ├── cilium.yaml
│   │   │   ├── argocd.yaml
│   │   │   ├── cert-manager.yaml
│   │   │   └── monitoring.yaml
│   │   └── cluster.yaml
│   └── stage-01/
├── infrastructure/
│   ├── cilium/
│   ├── argocd/
│   ├── kubelet-csr-approver/
│   ├── cert-manager/
│   ├── external-secrets/
│   ├── ingress/
│   ├── storage/
│   ├── observability/
│   ├── etcd-backup/
│   └── policies/
├── platform/
│   ├── namespaces/
│   ├── quotas/
│   ├── rbac/
│   ├── operators/
│   └── system-applications/
└── applications/
```

Каталог `clusters/<cluster>/values/` является единственным источником values и для bootstrap-этапов Ansible (Cilium, Argo CD), и для Argo CD Applications.

---

# 20. Слои синхронизации Argo CD

## Wave -20. Argo CD configuration

* AppProjects (включая `platform-root`);
* repository credentials (через ExternalSecret после появления ESO);
* RBAC;
* SSO;
* notifications;
* ApplicationSets;
* takeover самого Argo CD (декларативное владение bootstrap-установкой).

## Wave -10. Базовая инфраструктура

* Cilium — takeover по процедуре раздела 4.3;
* kubelet-csr-approver;
* cert-manager;
* External Secrets Operator;
* CSI;
* StorageClass;
* ingress controller или Gateway API controller.

## Wave 0. Системные сервисы

* metrics-server;
* observability agents (включая отгрузку audit-логов apiserver);
* policy engine;
* node problem detector;
* reloader;
* etcd-backup CronJob (см. раздел 28);
* backup agents.

## Wave 10. Platform configuration

* namespaces;
* quotas;
* RBAC;
* network policies;
* default LimitRange;
* PriorityClass;
* platform operators.

## Wave 20. Пользовательские приложения

* shared services;
* team applications;
* application operators.

Порядок задаётся аннотацией:

```yaml
metadata:
  annotations:
    argocd.argoproj.io/sync-wave: "-10"
```

---

# 21. Перечень компонентов первоначальной настройки

## 21.1. Обязательные компоненты

1. CNI;
2. Argo CD;
3. kubelet-csr-approver;
4. cert-manager;
5. External Secrets Operator;
6. CSI-драйвер;
7. default StorageClass;
8. ingress controller или Gateway API;
9. metrics-server;
10. observability agents;
11. policy engine;
12. etcd backup (CronJob + внешнее хранилище);
13. базовые NetworkPolicy;
14. default ResourceQuota;
15. default LimitRange;
16. PodDisruptionBudget для системных сервисов;
17. PriorityClass для platform-компонентов.

## 21.2. Компоненты, требующие отдельного ADR

* service mesh;
* kube-proxy replacement;
* multi-cluster networking / cluster mesh;
* централизованный (hub) Argo CD для любых окружений;
* автоматический admission mutation пользовательских Pod;
* сложные policy bundles;
* platform portal;
* application operators, не обязательные для работы кластера.

---

# 22. Secret management

GitOps-репозиторий не должен содержать plaintext-секреты.

Целевая схема:

```text
OpenBao
   ↓
External Secrets Operator
   ↓
Kubernetes Secret (зашифрован в etcd — раздел 14.1)
   ↓
Application
```

В Git хранится только декларация `ExternalSecret`:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: example-secret
  namespace: application
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: openbao
    kind: ClusterSecretStore
  target:
    name: example-secret
  data:
    - secretKey: password
      remoteRef:
        key: applications/example
        property: password
```

---

# 23. Валидация установки

Playbook считается успешно выполненным только после автоматической проверки (роль `cluster_validation`).

## 23.1. Узлы

```bash
kubectl get nodes
```

Критерии: все узлы зарегистрированы и `Ready`, роли и labels (`node-pool`) назначены, версия kubelet соответствует целевой.

## 23.2. Control plane

```bash
kubectl get --raw='/readyz?verbose'
kubectl get --raw='/livez?verbose'
kubectl get pods -n kube-system
```

## 23.3. Kubelet serving certificates

* pending kubelet-serving CSR аппрувнуты (bootstrap-шаг раздела 14.4);
* после установки kubelet-csr-approver — новые CSR аппрувятся автоматически;
* `kubectl get --raw "/api/v1/nodes/<node>/proxy/metrics"` через metrics-server работает без ошибок TLS.

## 23.4. Шифрование и аудит

* новые Secret в etcd имеют префикс `k8s:enc:<провайдер>:v1:` — проверка через `etcdctl get` тестового секрета, созданного и удаляемого в ходе валидации; в открытом виде значение не встречается;
* apiserver действительно запущен с `--encryption-provider-config`, `--audit-policy-file` и `--admission-control-config-file` — проверяется по манифесту static pod, поскольку ошибка в `extraArgs` приводит к молчаливому старту без этих флагов;
* `/var/log/kubernetes/audit/audit.log` пишется и непуст, события отгружаются в центральное хранилище.

## 23.4.1. Срок действия PKI

* `kubeadm certs check-expiration` на каждом control-plane узле;
* остаток менее 90 дней — предупреждение, менее суток — отказ валидации (раздел 27.1).

## 23.5. etcd

* три member, все healthy;
* endpoint доступны;
* тестовый snapshot создаётся успешно.

## 23.6. Сеть

* Pod-to-Pod, Pod-to-Service, Pod-to-external (внутренний контур), DNS;
* NetworkPolicy;
* MTU;
* `cilium status`;
* `cilium connectivity test` с профилем исключений из раздела 17, с последующей очисткой namespace.

## 23.7. Argo CD

```bash
kubectl get pods -n argocd
kubectl get applications -n argocd
```

Критерии: Pod'ы Ready; root Application `Synced` и `Healthy`; дочерние Applications созданы; Cilium Application после takeover — `Synced` без реальных диффов в spec; нет неизвестных `OutOfSync` ресурсов.

## 23.8. Storage

1. создать PVC;
2. запустить Pod;
3. записать данные;
4. перезапустить Pod;
5. проверить данные;
6. переподключить том на другой worker.

## 23.9. Перезагрузка

1. перезагрузить один worker → проверить возврат;
2. перезагрузить один control-plane узел → проверить API, etcd quorum;
3. остановить haproxy на master LB → проверить VRRP failover и доступность API через VIP;
4. убедиться, что Argo CD продолжает reconciliation.

---

# 24. Идемпотентность

Повторный запуск playbook не должен:

* повторно выполнять `kubeadm init`;
* создавать новый кластер;
* генерировать новые сертификаты без необходимости;
* перегенерировать ключ шифрования etcd;
* сбрасывать существующие узлы;
* перезаписывать kubeconfig;
* менять Pod/Service CIDR;
* переустанавливать CNI с другими параметрами и вмешиваться в ресурсы после takeover их Argo CD;
* пересоздавать Git credentials;
* нарушать работу Argo CD.

Для каждого необратимого действия — проверки состояния:

```yaml
- name: Check whether control plane is initialized
  ansible.builtin.stat:
    path: /etc/kubernetes/admin.conf
  register: kubernetes_admin_conf

- name: Initialize first control-plane node
  ansible.builtin.command:
    cmd: kubeadm init --config /etc/kubernetes/kubeadm-init.yaml --upload-certs
  when: not kubernetes_admin_conf.stat.exists
```

Одного `creates:` недостаточно; для критичных операций применяются отдельные pre-check и post-check. Роли `cilium_bootstrap` и `argocd_bootstrap` дополнительно проверяют, принят ли компонент под управление Argo CD (наличие Application в статусе `Synced`), и в этом случае пропускают применение манифестов полностью.

---

# 25. Тестирование

Проект проверяется тремя слоями: статическим анализом на каждый MR, прогоном на стенде из виртуальных машин и регламентными сценариями на нём же. Контейнерные сценарии (Molecule) из проекта удалены — практика показала, что стенд из ВМ находит то, чего контейнер не воспроизводит, а поддержка второго стенда стоила дороже пользы.

## 25.1. Статические проверки

```text
yamllint
ansible-lint
ansible-playbook --syntax-check
scripts/check-assert-conditions.py
```

Запускаются одной командой `make lint`, обязательны на каждый MR.

Последняя проверка написана по следам конкретного отказа: `: ` внутри незакавыченного условия `assert` превращает его в отображение YAML, условие молча перестаёт проверяться, и ни yamllint, ни ansible-lint, ни `--syntax-check` этого не видят.

Линтеры запускать **в версиях из `requirements-test.txt`**. На ansible-lint 6.x проект даёт ложные ошибки: схема той версии не знает Ubuntu noble, а без `netaddr` не работает фильтр `ipaddr` в роли `preflight`.

## 25.2. Стенд из виртуальных машин

Минимальная топология — три control-plane и два worker-узла; пятый узел держится вне inventory и вводится отдельно, чтобы проверить расширение кластера.

Обязательный набор перед выпуском изменений:

| Что | Чем проверяется |
| --- | --------------- |
| установка с нуля | `site.yml` целиком, одной командой |
| расширение | `maintenance/add-node.yml` |
| вывод узла | `maintenance/remove-node.yml` |
| продление PKI | `maintenance/renew-control-plane-certs.yml` |
| ротация ключа шифрования | `maintenance/rotate-etcd-encryption-key.yml`, все четыре фазы |
| резервное копирование | `80-backup.yml` с ожиданием срабатывания таймера |
| восстановление | `maintenance/restore-etcd.yml` |
| обновление | `upgrade/`, по порядку номеров |

Проверять результат следует на живом кластере, а не по коду возврата Ansible: узлы `Ready`, кворум etcd, префикс шифрования записей в etcd, возврат манифестов статических подов, отсутствие подов вне `Running`.

Ротацию ключа и восстановление нельзя считать проверенными без контрольных объектов. Для ротации — секрет, записанный до неё и прочитанный после. Для восстановления — объекты, созданные ПОСЛЕ снятия снимка: если они не исчезли, восстановление не состоялось.

## 25.3. Чего стенд в облаке не проверяет

| Что | Препятствие |
| --- | ----------- |
| роль `haproxy_keepalived` вживую | произвольный VIP на L2 в облаке не поднять; нужен bare-metal или сеть с поддержкой VRRP |
| отдельный диск под etcd | требует ВМ с дополнительным томом |
| работа через внутренние зеркала и Harbor | нужен закрытый контур |
| OpenBao как секрет-бэкенд | нужен OpenBao |
| приватный GitOps-репозиторий по SSH | нужен deploy key |
| отказные пути `preflight` | прогон идёт по счастливому пути; проверяется подстановкой заведомо неверных значений вручную |

Эти пункты закрываются на предпродуктивном контуре, а не на облачном стенде.

# 26. Повторное присоединение узлов

Bootstrap token не хранится постоянно. При добавлении узла Ansible:

1. запрашивает состояние кластера;
2. создаёт новый token (`kubeadm token create --ttl 30m --print-join-command`);
3. получает CA hash;
4. при control-plane join загружает сертификаты (`kubeadm init phase upload-certs --upload-certs`);
5. выполняет join;
6. удаляет или дожидается истечения token;
7. проверяет новый узел, включая аппрув его kubelet-serving CSR (автоматически через kubelet-csr-approver).

---

# 27. Обновление Kubernetes

Upgrade оформляется отдельным playbook:

```text
playbooks/upgrade/
├── 00-preflight.yml
├── 10-upgrade-first-control-plane.yml
├── 20-upgrade-other-control-plane.yml
├── 30-upgrade-workers.yml
└── 40-validation.yml
```

Порядок:

1. проверить compatibility matrix (Kubernetes ↔ containerd ↔ Cilium);
2. создать etcd snapshot;
3. проверить PodDisruptionBudget;
4. обновить пакет kubeadm на первом control-plane;
5. `kubeadm upgrade plan`;
6. **первый control-plane узел**: `kubeadm upgrade apply v1.xx.y`;
7. обновить kubelet и kubectl на первом узле, рестарт kubelet;
8. **остальные control-plane узлы, последовательно**: `kubeadm upgrade node` (не `apply`), затем kubelet;
9. **worker-узлы, последовательно**: `kubectl drain` → обновление пакетов → `kubeadm upgrade node` → рестарт kubelet → `kubectl uncordon`;
10. обновить `sandbox_image` в containerd, если новая версия kubeadm ожидает другой pause;
11. полная валидация (раздел 23).

Minor-версии Kubernetes нельзя пропускать. Обновление Cilium и Argo CD выполняется отдельно, через GitOps, до или после upgrade Kubernetes согласно compatibility matrix.

## 27.1. Продление PKI control plane

Сертификаты, выпускаемые kubeadm, живут **один год**. Это отдельный регламент, а не часть upgrade.

`rotateCertificates: true` в KubeletConfiguration покрывает только клиентские сертификаты kubelet, а serving-сертификаты обслуживает kubelet-csr-approver. Сертификаты apiserver, etcd и front-proxy не продлеваются сами: они обновляются либо явной командой `kubeadm certs renew`, либо неявно при `kubeadm upgrade apply`. Кластер, который год не апгрейдился и не обслуживался, умирает тихо — apiserver перестаёт принимать соединения, и восстановление требует ручного вмешательства на каждом узле.

Регламент:

* **проверка** — `kubeadm certs check-expiration` включена в роль `cluster_validation`, предупреждение при остатке менее 90 дней;
* **алерт** — на метрику `apiserver_client_certificate_expiration_seconds`, порог согласуется с дежурной сменой;
* **продление** — отдельный playbook, выполняется по одному узлу:

```bash
ansible-playbook playbooks/maintenance/renew-control-plane-certs.yml
```

Порядок на каждом узле: резервная копия `/etc/kubernetes/pki` и kubeconfig-файлов → `kubeadm certs renew all` → перезапуск static pods перемещением манифестов из `/etc/kubernetes/manifests` и обратно → ожидание `/readyz`.

Перезапуск именно перемещением манифеста, а не `crictl rm`: kubelet удаляет Pod при исчезновении файла и создаёт заново при появлении, тогда как удалённый контейнер он может пережить кэшем.

Периодичность — не реже раза в 6 месяцев, чтобы окно продления никогда не сходилось со сроком истечения.

---

# 28. Backup и восстановление

## 28.1. Регулярные etcd snapshots — владелец процесса

Резервное копирование etcd — постоянный регламент, а не разовое действие playbook:

* **механизм**: CronJob `etcd-backup` (устанавливается через GitOps, wave 0), выполняющий `etcdctl snapshot save` на control-plane узле и выгружающий snapshot во внешнее S3-хранилище (MinIO/корпоративное), вне кластера. Обязательные детали манифеста, которые легко упустить:
  * `hostNetwork: true` — без него `127.0.0.1:2379` указывает внутрь сетевого namespace Pod'а, и до etcd достучаться не выйдет;
  * `tolerations` на taint control-plane и `nodeSelector` по `node-role.kubernetes.io/control-plane` — иначе Pod никуда не запланируется;
  * hostPath к `/etc/kubernetes/pki/etcd` только на чтение;
  * snapshot не должен оставаться внутри `/var/lib/etcd`: он попадёт в следующий snapshot и будет расти рекурсивно;
* **альтернатива** при запрете hostPath: systemd timer на каждом control-plane узле, настраиваемый ролью `etcd_backup` Ansible;
* расписание: не реже 1 раза в сутки для prod; retention согласуется с ИБ;
* мониторинг: алерт на отсутствие свежего snapshot (метрика возраста последнего успешного бэкапа);
* ежеквартально — тестовое восстановление snapshot на стенде.

Выбор механизма (CronJob или systemd timer) фиксируется в ADR; по умолчанию — CronJob, так как он управляется через Git.

## 28.2. Что резервируется

* etcd snapshots (регламент выше);
* ключ шифрования etcd (в OpenBao; **без него snapshot не восстановим** — Secrets зашифрованы). Если snapshot снят во время ротации ключа (раздел 14.1), для восстановления нужны **оба** ключа — старый и новый;
* kubeadm configuration;
* PKI control plane;
* Ansible inventory;
* GitOps repository;
* credentials recovery procedure;
* конфигурация Load Balancer (haproxy.cfg, keepalived.conf или конфигурация внешнего LB);
* конфигурация DNS;
* данные stateful-приложений — отдельными средствами.

## 28.3. Git не заменяет etcd backup

Git содержит желаемое состояние декларативных ресурсов, но не содержит: runtime-состояние, динамически созданные Secret, status объектов, Lease, часть данных операторов, данные приложений.

## 28.4. Восстановление кластера

```text
Создать новые VM
      ↓
Ansible: LB + OS preparation
      ↓
Восстановить control plane (etcd restore) или создать новый
      ↓
Bootstrap CNI
      ↓
Bootstrap Argo CD
      ↓
Применить root Application
      ↓
Argo CD восстанавливает cluster configuration
      ↓
Восстановить stateful data
```

Восстановление etcd автоматизировано: `playbooks/maintenance/restore-etcd.yml`. Снимок разворачивается на **всех** control-plane узлах разом — восстановить один член из снимка нельзя, остальные сохранят прежнюю историю и отвергнут его. Все три члена стартуют как новый кластер с новым cluster ID, но со старыми данными. Прежний каталог данных не удаляется, а переименовывается. Проверка целостности снимка выполняется до остановки control plane, а возврат манифестов статических подов — в секции `always`.

Для большинства platform-компонентов предпочтителен сценарий «новый кластер из Git». Восстановление etcd требуется при критичном внутреннем состоянии (динамические Secret, данные операторов). При etcd restore обязательно наличие исходного ключа шифрования из OpenBao. Схема с in-cluster Argo CD делает восстановление автономным: внешний management-кластер не требуется.

---

# 29. Безопасность

## 29.1. SSH

* отдельный пользователь automation;
* вход по ключам;
* запрет password authentication;
* ограниченный sudo;
* журналирование действий;
* ротация ключей.

## 29.2. Kubernetes PKI и данные

* закрытые ключи не сохраняются в Git;
* kubeconfig с правами cluster-admin — в защищённом месте;
* bootstrap tokens имеют ограниченный TTL;
* certificate key не логируется;
* доступ к `/etc/kubernetes/pki` ограничен;
* Secrets в etcd зашифрованы (раздел 14.1), ключ — в OpenBao;
* API audit log включён и отгружается в центральное хранилище.

## 29.3. Ansible secrets

* Ansible Vault или OpenBao lookup plugin;
* CI secret variables;
* ephemeral credentials.

`no_log: true` обязателен для задач, содержащих: token, private key, kubeconfig, password, Git credential, certificate key, etcd encryption key.

## 29.4. Argo CD

После bootstrap:

* подключить SSO;
* отключить или ограничить встроенного admin;
* настроить RBAC;
* создать отдельные AppProject, включая `platform-root` с запретом delete;
* ограничить source repositories;
* ограничить destination namespaces;
* ограничить cluster-scoped resources;
* включить audit logging;
* не предоставлять командам доступ к default project.

---

# 30. Definition of Done

Кластер считается введённым в эксплуатацию, когда:

* все VM подготовлены Ansible;
* control-plane endpoint отказоустойчив, failover VIP протестирован;
* работают минимум три control-plane узла;
* etcd имеет quorum;
* Secrets в etcd зашифрованы, ключ размещён в OpenBao;
* API audit log включён и отгружается;
* все worker-узлы `Ready`;
* CNI прошёл connectivity test (профиль закрытого контура), namespace теста удалён;
* CNI принят под управление Argo CD, Application `Synced`;
* kubelet-serving CSR аппрувятся автоматически, metrics-server работает;
* CoreDNS работает;
* Argo CD синхронизирован с Git, root Application защищён от каскадного удаления;
* все обязательные platform-компоненты управляются через Argo CD;
* настроено получение секретов из OpenBao;
* работает storage provisioning;
* работает ingress или Gateway API;
* установлены observability agents;
* настроены базовые policies;
* работает регулярный etcd backup, алерт на отсутствие snapshot активен;
* включён Pod Security Admission с baseline-дефолтом;
* настроен алерт на срок действия сертификатов control plane, регламент продления PKI зафиксирован (раздел 27.1);
* протестирована перезагрузка worker;
* протестирована потеря одного control-plane узла;
* протестирован failover Load Balancer;
* подготовлены инструкции: добавление/удаление узла, обновление Kubernetes, восстановление, ротация ключа шифрования etcd, продление PKI control plane;
* статические проверки зелёные в CI, прогон `site.yml` с нуля на стенде из ВМ пройден без отказов, регламентные сценарии раздела 25.2 проверены на живом кластере;
* отсутствуют ручные кластерные изменения, не отражённые в Git.

---

# 31. Запуск playbook

Полная установка:

```bash
ansible-playbook -i inventories/prod-01/hosts.yml playbooks/site.yml
```

Только preflight:

```bash
ansible-playbook -i inventories/prod-01/hosts.yml playbooks/00-preflight.yml
```

Bootstrap Argo CD:

```bash
ansible-playbook -i inventories/prod-01/hosts.yml playbooks/60-argocd-bootstrap.yml
```

Валидация:

```bash
ansible-playbook -i inventories/prod-01/hosts.yml playbooks/70-validation.yml
```

Регламентные операции:

```bash
# Добавление узла (узел предварительно описан в inventory)
ansible-playbook playbooks/maintenance/add-node.yml --limit k8s-worker-04

# Вывод узла, включая удаление member etcd для control-plane
ansible-playbook playbooks/maintenance/remove-node.yml -e node_to_remove=k8s-worker-04

# Продление PKI control plane (раздел 27.1)
ansible-playbook playbooks/maintenance/renew-control-plane-certs.yml

# Ротация ключа шифрования etcd — по одной фазе за запуск (раздел 14.1)
ansible-playbook playbooks/maintenance/rotate-etcd-encryption-key.yml -e rotation_phase=1
```

---

# 32. Рекомендуемый состав `site.yml`

```yaml
---
- import_playbook: 00-preflight.yml
- import_playbook: 05-load-balancer.yml
- import_playbook: 10-os-prepare.yml
- import_playbook: 20-containerd.yml
- import_playbook: 30-kubernetes-packages.yml
- import_playbook: 40-control-plane-init.yml
- import_playbook: 41-control-plane-join.yml
- import_playbook: 42-workers-join.yml
- import_playbook: 50-cni-bootstrap.yml
- import_playbook: 60-argocd-bootstrap.yml
- import_playbook: 70-validation.yml
```

`05-load-balancer.yml` внутри содержит условие `api_vip_managed`.

---

# 33. Ключевое архитектурное решение

Ansible отвечает за состояние узлов и базовую установку Kubernetes.

Argo CD (in-cluster, per-cluster для prod) отвечает за состояние ресурсов внутри Kubernetes.

Граница ответственности:

```text
Ansible:
VM → LB/VIP → OS → kernel → containerd → kubelet → kubeadm → CNI bootstrap → Argo CD bootstrap

Argo CD:
Argo CD self-management → CNI ownership → csr-approver → storage → ingress → secrets → observability → policies → platform → applications
```

Изменения внутри Kubernetes после bootstrap выполняются через pull request в GitOps-репозиторий.

Прямое применение ресурсов через `kubectl apply` допускается только:

* при аварийном восстановлении;
* в рамках документированной break-glass процедуры;
* с обязательным последующим внесением идентичного изменения в Git;
* с фиксацией действия в журнале изменений.
