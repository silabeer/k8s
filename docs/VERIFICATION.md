# Как проверить самому

Пошаговые сценарии проверки кластера и регламентных операций. Написаны по
следам фактических прогонов: всё описанное выполнялось на живых машинах, и
почти каждая ловушка в разделе 1 стоила отдельного цикла разбора.

Что уже проверено и с каким результатом — в `TEST-PLAN.md`. Здесь —
инструкции, чтобы повторить.

---

## 1. Общие правила. Прочитать до начала

Эти правила важнее самих сценариев. Нарушив любое, вы получите зелёный
результат, ничего не проверив.

### 1.1. Зелёный вывод Ansible не означает, что работа сделана

Playbook отчитывается о выполнении задач, а не о достижении цели. Роль
может пройти без единой ошибки и не настроить ничего: так было с OpenBao,
где отсутствие токена молча пропускало включение хранилища секретов, а
финальная задача печатала «готово».

**Проверяйте результат на системе, а не вывод в терминале.**

### 1.2. Проверять доступ к API нужно ИЗНУТРИ ПОДА

`kubectl` с узла обращается к apiserver напрямую по адресу узла, минуя
ClusterIP. Целый класс отказов — сломанная сервисная маршрутизация — с
хоста не виден вовсе.

```bash
kubectl run probe --image=busybox:1.36 --restart=Never --command -- sleep 300
kubectl exec probe -- nc -w 3 -z <ClusterIP сервиса kubernetes> 443 && echo OK
```

Однажды кластер 25 минут выглядел здоровым, пока поды не могли достучаться
до API.

### 1.3. При `loop` Ansible пишет `failed:`, а не `fatal:`

Если вы ищете отказы командой `grep '^fatal'`, задачи с циклом пройдут мимо
вас, и сработавшая проверка будет засчитана как несработавшая. Ищите оба:

```bash
grep -E "^fatal:|^failed:" прогон.log
```

### 1.4. Glob раскрывается ДО sudo

```bash
sudo ls /var/backups/etcd/*.db      # каталог 0700 — «No such file or directory»
sudo sh -c 'ls /var/backups/etcd/*.db'   # верно
```

Первая форма даёт пустой результат и создаёт впечатление, что файлов нет.

### 1.5. Сертификаты сравнивать по отпечатку, а не по субъекту

Общий центр сертификации Cilium и сгенерированный чартом называются
одинаково — `CN = Cilium CA`. По имени они неразличимы.

```bash
kubectl -n kube-system get secret cilium-ca -o jsonpath='{.data.ca\.crt}' \
  | base64 -d | openssl x509 -noout -fingerprint -sha256
```

### 1.6. Проверять отказной путь обязательно

Политика, которая ничего не запрещает, выглядит работающей. Убедитесь, что
**неразрешённое действительно не проходит**, а не только что разрешённое
проходит. Это относится к сетевым политикам, PSA, mutual authentication и
любым проверкам preflight.

### 1.7. Сначала убедитесь, что исправен сам тест

Заглушка на `nc` в busybox не слушала порт, и выводы о Cluster Mesh
делались по сломанному тесту. Перед проверкой связности убедитесь, что
целевой сервис отвечает напрямую:

```bash
kubectl exec <клиент> -- wget -T 4 -qO- http://<IP пода сервера>/
```

### 1.8. Проверки снаружи выполнять в обход прокси

Если на вашей машине настроен HTTP-прокси, `curl` уйдёт в него и
`--resolve` будет проигнорирован. Проверка HTTPS-входа так давала
`HTTP 000` со всех узлов и выглядела как отказ шлюза, хотя отказывал
прокси. Во всех проверках «снаружи» добавляйте `--noproxy '*'`.

### 1.9. Синхронизацию Argo CD без CLI запускать только с syncOptions

Если CLI `argocd` недоступен, синхронизацию запускают патчем поля
`operation`. Argo CD **не подставляет** в такую операцию `syncOptions`
из самого приложения, поэтому их нужно перечислить руками:

```bash
kubectl -n argocd patch app <name> --type merge -p '{"operation":{
  "sync":{"revision":"HEAD",
          "syncOptions":["CreateNamespace=true","ServerSideApply=true"]},
  "initiatedBy":{"username":"<кто>"}}}'
```

Без них теряется `CreateNamespace=true`: namespace не создаётся, все
задачи падают с `namespaces "<ns>" not found`, а приложение показывает
`health=Healthy` — просто потому, что здоровых ресурсов у него нет.
Признак именно этой причины: в `syncResult` **нет задачи с `Namespace`**.
Список опций берите из `spec.syncPolicy.syncOptions` приложения.

---

## 2. Установка кластера

**Цель:** убедиться, что кластер собирается с нуля и работоспособен.

```bash
ansible-playbook -i inventories/<контур>/hosts.yml \
  inventories/<контур>/00-stand-prerequisites.yml     # только для стенда
ansible-playbook -i inventories/<контур>/hosts.yml playbooks/site.yml
```

**Проверка на живом кластере:**

```bash
kubectl get nodes -o wide                     # все Ready
kubectl get pods -A | grep -v Running         # пусто
kubectl -n kube-system get ds cilium          # ready == desired
```

Шифрование Secrets в etcd — не по конфигурации, а по факту:

```bash
kubectl -n default create secret generic probe --from-literal=k=v
kubectl -n kube-system exec etcd-<узел> -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  get /registry/secrets/default/probe | head -2
```

Ожидается префикс `k8s:enc:secretbox:v1:<имя ключа>`. Если данные читаются
как открытый текст — шифрование не работает.

---

## 3. Шифрование трафика WireGuard

**Цель:** убедиться, что трафик между подами разных узлов действительно
зашифрован, а не только заявлен таковым в статусе.

Поднимите два пода **на разных узлах** (`nodeName` в манифесте), затем на
одном из узлов:

```bash
sudo timeout 20 tcpdump -i eth0 -nn \
  'host <адрес второго узла> and (udp port 51871 or udp port 8472)' -w /tmp/w.pcap
# в это время генерируйте трафик между подами
sudo tcpdump -nn -r /tmp/w.pcap 'udp port 51871' | wc -l   # много
sudo tcpdump -nn -r /tmp/w.pcap 'udp port 8472'  | wc -l   # ноль
```

Ноль на 8472 означает, что туннель упакован в WireGuard целиком.

**На что не купиться.** В статусе агента на control-plane узлах будет
`NodeEncryption: OptedOut` — это штатное исключение по метке
`node-role.kubernetes.io/control-plane`, а не поломка. Трафик между подами
шифруется и там.

---

## 4. Cluster Mesh

**Цель:** проверить, что сервисы видят друг друга между кластерами и
переживают потерю площадки.

```bash
POD=$(kubectl -n kube-system get pods -l k8s-app=cilium -o jsonpath='{.items[0].metadata.name}')
kubectl -n kube-system exec $POD -c cilium-agent -- cilium-dbg status --all-clusters
```

Ищите `ready` у соседнего кластера и `synchronization status` со всеми
`true`.

**Глобальный сервис.** Разверните одинаковый по имени и namespace сервис в
обоих кластерах с аннотацией:

```yaml
metadata:
  annotations:
    service.cilium.io/global: "true"
    service.cilium.io/affinity: local
```

Проверка — тремя шагами, из пода одного кластера:

```bash
CIP=$(kubectl -n default get svc <имя> -o jsonpath='{.spec.clusterIP}')
for i in $(seq 10); do kubectl exec <клиент> -- wget -qO- http://$CIP/; echo; done
# с affinity: local — все ответы своего кластера
kubectl scale deploy <сервис> --replicas=0        # гасим локальные
sleep 25
for i in $(seq 5); do kubectl exec <клиент> -- wget -qO- http://$CIP/; echo; done
# ответы приходят из СОСЕДНЕГО кластера
```

**На что не купиться.** `affinity: local` **скрывает удалённые backend'ы**
из вывода `cilium-dbg service list`, пока жив хоть один локальный. Один
backend в списке не означает, что mesh не собрался. Судите по ответам либо
снимите аннотацию.

### 4.1. Разрыв связи между площадками

Обратимо, без вмешательства в облако — правилами пакетного фильтра на
узлах одного кластера:

```bash
sudo iptables -I INPUT 1 -s <подсеть соседа> -j DROP
sudo iptables -I OUTPUT 1 -d <подсеть соседа> -j DROP
# снять:
sudo iptables -D INPUT -s <подсеть соседа> -j DROP
sudo iptables -D OUTPUT -d <подсеть соседа> -j DROP
```

Ожидается: оба кластера живы, каждый обслуживает свой сервис локально,
после снятия правил mesh сходится сам.

**На что не купиться — два пункта, оба существенные.**

`cilium-dbg status --all-clusters` **всё время разрыва показывает
`ready`**. При KVStoreMesh агенты подключаются к локальному
`clustermesh-apiserver`, и статус отражает состояние локального кеша, а не
связь с площадкой. Настоящий разрыв виден в логах контейнера
`kvstoremesh`. **Оповещение о потере связи на этом статусе строить
нельзя.**

Во время разрыва трафик глобального сервиса **продолжает уходить на
исчезнувшие backend соседа**: обновления не доходят. В измеренном случае
два запроса из восьми завершились отказом. Деградация частичная.

---

## 5. Регламентные операции

### 5.1. Восстановление etcd из снимка

**Цель:** убедиться, что откат настоящий, а не видимость.

Ключ проверки — **контрольные объекты, созданные ПОСЛЕ снимка**. Без них
вы не отличите восстановление от его отсутствия.

```bash
sudo systemctl start etcd-backup.service     # снимок
kubectl create ns after-snapshot
kubectl -n default create configmap canary --from-literal=w=after
kubectl scale deploy <любой> --replicas=3    # в снимке было другое число

ansible-playbook -i inventories/<контур>/hosts.yml \
  playbooks/maintenance/restore-etcd.yml \
  -e restore_snapshot_path=/var/backups/etcd/<файл>.db \
  -e restore_confirm=<cluster_name>
```

Ожидается: `after-snapshot` и `canary` — `NotFound`, число реплик вернулось
к значению из снимка, узлы `Ready`, ни одного пода вне `Running`.

**Обязательно проверить доступ к API изнутри пода** (правило 1.2): именно
здесь обнаружился отказ, невидимый с хоста.

### 5.2. Ротация ключа шифрования etcd

Четыре фазы, **каждая отдельным запуском**, с проверкой между ними:

```bash
for phase in 1 2 3 4; do
  ansible-playbook ... playbooks/maintenance/rotate-etcd-encryption-key.yml \
    -e rotation_phase=$phase
  # между фазами: kubectl get secrets -A должен работать
done
```

Контроль — секрет, записанный **до** ротации: он обязан читаться после
неё. И префикс в etcd должен смениться на новое имя ключа после фазы 3.

**Осторожно.** Имя ключа входит в префикс записи, и apiserver подбирает
ключ расшифровки по имени. После ротации имя обязано следовать за ключом в
секрет-бэкенде, иначе следующий штатный прогон сделает все Secrets
нечитаемыми.

### 5.3. Продление сертификатов control plane

```bash
ansible-playbook ... playbooks/maintenance/renew-control-plane-certs.yml
```

Проверка: `kubeadm certs check-expiration` показывает новые сроки, все
четыре манифеста статических подов на месте, каталога
`/etc/kubernetes/manifests-restart` не существует.

Последнее важно: если каталог остался, манифесты застряли вне рабочего
каталога, и control plane неполон.

### 5.4. Ввод и вывод узла

```bash
ansible-playbook ... playbooks/maintenance/add-node.yml -e new_node=<имя>
ansible-playbook ... playbooks/maintenance/remove-node.yml -e node_to_remove=<имя>
```

После вывода: узел исчез, нагрузка переехала, `kubelet` на нём остановлен и
отключён, `desired` у DaemonSet Cilium уменьшился. Под-сирота собирается
мусорщиком в течение пары минут — не пугайтесь, если сразу он ещё виден.

---

## 6. Отказные пути

Самая недооценённая часть. Прогоны по счастливому пути дают меньше
уверенности, чем один отказной.

### 6.1. Проверки preflight

Подставьте заведомо неверные значения и убедитесь, что каждая срабатывает:

```bash
ansible-playbook ... playbooks/00-preflight.yml -e cluster_dns=10.55.0.10
ansible-playbook ... playbooks/00-preflight.yml -e service_network_cidr=<равный pod CIDR>
ansible-playbook ... playbooks/00-preflight.yml -e secret_backend=hashicorp
ansible-playbook ... playbooks/00-preflight.yml -e node_mtu=9000
ansible-playbook ... playbooks/00-preflight.yml -e preflight_min_kernel_version=99.0
```

Каждая обязана дать внятное сообщение. Помните про правило 1.3: ищите и
`failed:`, и `fatal:`.

### 6.2. Потеря control-plane

Обратимо, без выключения машины:

```bash
sudo mv /etc/kubernetes/manifests/kube-apiserver.yaml /root/   # погасить
sudo mv /root/kube-apiserver.yaml /etc/kubernetes/manifests/   # вернуть
```

Ожидается: рабочие нагрузки продолжают работать, в mesh соседний кластер
продолжает получать ответы от подов этого кластера.

---

## 6a. Gateway API

**Цель:** убедиться, что вход снаружи работает, маршрутизация идёт по
правилам `HTTPRoute`, а TLS завершается сертификатом cert-manager.

**Предусловия.** `kubeProxyReplacement: true` (ADR 0001). CRD Gateway API
standard-канала **и `TLSRoute` из experimental** — без него оператор
Cilium валится на каждой реконсиляции.

### 6a.1. Реализация зарегистрирована

```bash
kubectl get gatewayclass
kubectl get gatewayclass cilium -o jsonpath='{.status.conditions[*].reason}'
```

Ожидается класс `cilium` с контроллером `io.cilium/gateway-controller` и
`Accepted`. **Пустой список — самый вероятный отказ.** Он не значит, что
Cilium сломан: `cilium status` при этом чистый, а `enable-gateway-api` в
`cilium-config` стоит `true`. Проверьте, что в values задано явное
`gatewayAPI.gatewayClass.create: "true"` — при умолчании `auto` объект не
создаётся никогда, потому что роль вызывает `helm template` без
подключения к кластеру.

### 6a.2. Gateway получил адрес

```bash
kubectl -n <ns> get gateway <имя> \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'
```

`Programmed=False AddressNotAssigned` означает, что адрес выдавать некому:
нужен либо облачный LoadBalancer, либо `CiliumLoadBalancerIPPool`. На
стенде адрес из пула виден изнутри, но **снаружи не доступен** — его
некому анонсировать; для этого нужны L2-анонсы или BGP. Вход снаружи в
таком случае проверяется через NodePort сервиса `cilium-gateway-<имя>`.

### 6a.3. Маршрутизация

Обязательно проверяются оба пути — совпадающий и нет:

```bash
curl --noproxy '*' -o /dev/null -w '%{http_code}\n' http://<узел>:<nodePort>/gw
curl --noproxy '*' -o /dev/null -w '%{http_code}\n' http://<узел>:<nodePort>/nomatch
```

Ожидается 200 и 404. Если оба дают 200 — правило не применяется и
маршрутизация не проверена.

**`503 upstream connect error ... connection timeout` — это чаще всего
сетевая политика, а не приложение.** Envoy обращается к поду с адреса
узла под идентичностью `reserved:ingress`, и базовый `default-deny-ingress`
его отбрасывает. Проверяется так:

```bash
kubectl -n kube-system exec <под cilium> -c cilium-agent -- \
  hubble observe --namespace <ns> --verdict DROPPED --last 20
```

Строка `Policy denied DROPPED` подтверждает причину. Лечится
`CiliumNetworkPolicy` с `fromEntities: [ingress]` — обычным
`NetworkPolicy` этот источник не выражается.

### 6a.4. TLS

```bash
kubectl -n <ns> get certificate            # ожидается READY=True
echo | openssl s_client -connect <узел>:<nodePort https> \
  -servername <имя из listener> 2>/dev/null | openssl x509 -noout -issuer -dates
curl --noproxy '*' --cacert <CA cert-manager> \
  --resolve <имя>:<порт>:<узел> https://<имя>:<порт>/gw
```

Отказные пути проверяются обязательно:

* без `--cacert` — `exit=60` (сертификат не проверен);
* с чужим именем в SNI — `exit=35` (подходящего listener нет).

Если оба варианта проходят успешно, TLS не проверен.

---

## 7. Mutual authentication

**Цель:** проверить, что идентичность действительно проверяется, и понять,
сколько времени у вас есть при отказе SPIRE.

**Предусловие:** namespace `cilium-spire` должен иметь метки PSA
`privileged` — чарт их не проставляет, и под профилем `baseline` агенты
SPIRE не стартуют вовсе.

Записи, созданные оператором Cilium:

```bash
kubectl -n cilium-spire exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server entry show -selector cilium:mutual-auth
```

**Проверка политики — обе стороны.** Разверните два клиента на узле,
отличном от сервера, разрешите политикой только одного:

```yaml
spec:
  endpointSelector: {matchLabels: {app: srv}}
  ingress:
    - fromEndpoints: [{matchLabels: {app: allowed}}]
      authentication: {mode: required}
```

Разрешённый обязан пройти, **неразрешённый — нет**. Второе важнее первого.

### 7.1. Отказ SPIRE

```bash
kubectl -n cilium-spire scale sts spire-server --replicas=0
```

Проверьте три категории и ожидайте разного:

| Что | Ожидание |
| --- | -------- |
| уже аутентифицированная пара | работает, пока действителен сертификат |
| новый под с той же идентичностью | работает |
| **новая идентичность** (другие метки) | **не проходит сразу** |

Возврат сервера восстанавливает трафик сам, без вмешательства.

### 7.2. Сколько времени есть на реакцию

Запас определяется **сроком жизни сертификата**, а не наличием кеша.
Проверить быстро можно, укоротив срок в конфигурации сервера SPIRE:

```bash
kubectl -n cilium-spire get cm spire-server -o jsonpath='{.data.server\.conf}'
# добавить в блок server: default_x509_svid_ttl = "2m"
kubectl -n cilium-spire rollout restart sts/spire-server ds/spire-agent
kubectl -n kube-system rollout restart ds/cilium
```

В измеренном случае при TTL в 2 минуты работавшая пара встала через **81
секунду**, возврат сервера восстановил трафик за **46 секунд**. При
значении по умолчанию (час) запас — порядка десятков минут.

Отсюда практический вывод: TTL это ручка компромисса. Длиннее — дольше
переживёте отказ SPIRE, но дольше живёт скомпрометированный сертификат.

---

## 8. Если проверка не сошлась

Прежде чем заводить дефект, исключите три самые частые причины ложного
результата — все три встречались в этом проекте:

1. **Тест сломан.** Убедитесь, что целевой сервис отвечает напрямую.
2. **Смотрите не туда.** Доступ к API — изнутри пода. Сертификаты — по
   отпечатку. Отказы в логе — и `failed:`, и `fatal:`.
3. **Проверка выполнена до применения изменений.** Обработчики Ansible
   срабатывают в конце play; если проверка стоит раньше, она видит старое
   состояние.
