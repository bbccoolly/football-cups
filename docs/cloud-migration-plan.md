# 阿里云杭州迁移与运行计划

> 版本：V1.1
> 状态：ECS 已创建，等待隔离 smoke
> 更新日期：2026-07-16

本文是从本地 Windows 验证环境迁移到阿里云 ECS 的权威操作文档。迁移不改变产品、数据和模型门禁；未满足正式切换条件时不得停止 Windows 采集，也不得启用云端长期 timer。

## 1. 实际环境与容量结论

当前 ECS 实际配置：

| 项目 | 实际值 |
| --- | --- |
| 地域 | `cn-hangzhou` |
| 系统 | Ubuntu 22.04.5 LTS x86_64 |
| 规格 | 2 vCPU / 4 GiB |
| 系统盘 | 40 GB，约 35 GB 可用 |
| 数据盘 | 尚未购买或挂载 |
| Swap | 尚未配置 |
| Python | 系统 3.10.12，项目要求 3.11+ |
| 网络 | NTP 正常；500 竞彩首页 HTTP 200 |

本地约 22 小时形成 54.3 MB 文件事实，粗略折算约 21 GB/年，尚未计入赛事高峰和 PostgreSQL。40 GB 系统盘只能承载系统、代码、虚拟环境、swap、受控日志和隔离 smoke；正式 timer 启用前必须增加至少 100 GB ESSD 数据盘。

Ubuntu 22.04 可用于当前阶段，但应在 2027-03-31 前完成 Ubuntu 24.04/Python 3.12 迁移或另行登记维护决策。

## 2. 三个运行状态

| 状态 | 数据目录 | 允许行为 | 禁止行为 |
| --- | --- | --- | --- |
| 系统盘 smoke | `/var/lib/football-cups-smoke/500` | 离线测试、单次发现、四市场和赛果 smoke | systemd timer、正式导入、计入 7/30 天验收 |
| 数据盘预切换 | `/srv/football-cups/data/500` | 恢复、重建、数据库导入、手工 service 验证 | 与 Windows 同时正式采集 |
| 云端正式运行 | `/srv/football-cups/data/500` | 单写入采集、数据库增量导入、7/30 天重新计时 | Windows 同时写入同一前瞻事实 |

smoke 数据与正式数据隔离，不导入正式 PostgreSQL，也不计入严格训练或云端连续验收。

## 3. 稳定配置与健康接口

正式环境必须设置：

```text
FOOTBALL_CUPS_DATA_DIR=/srv/football-cups/data/500
FOOTBALL_CUPS_REQUIRED_MOUNT=/srv/football-cups
COLLECTOR_DISK_WARNING_FREE_GB=50
COLLECTOR_DISK_CRITICAL_FREE_GB=20
COLLECTOR_DISK_WARNING_FREE_PERCENT=20
COLLECTOR_DISK_CRITICAL_FREE_PERCENT=10
COLLECTOR_HEALTH_HEARTBEAT_MAX_AGE_MINUTES=10
COLLECTOR_HEALTH_DISCOVERY_MAX_AGE_MINUTES=45
COLLECTOR_HEALTH_CLOCK_MAX_AGE_MINUTES=45
```

smoke 环境不设置 `FOOTBALL_CUPS_REQUIRED_MOUNT`，绝对磁盘阈值改为 10 GB/5 GB。正式 systemd 服务额外要求 `/srv/football-cups` 是真实挂载点；数据盘缺失时不得回落到系统盘创建同名目录。

`health` 状态和退出码：

| 状态 | 退出码 | 含义 |
| --- | ---: | --- |
| `ok` | 0 | SQLite、时效、磁盘和挂载门禁均通过 |
| `warning` | 1 | 初始化证据缺失、磁盘预警或任务积压，需要检查但未达到严重门槛 |
| `failed` | 3 | SQLite 损坏、挂载缺失、严重磁盘、时钟或运行证据过期 |

初始化后尚无心跳时出现 `warning` 是预期行为；完成一次成功 `run-once` 后，正式切换要求 `health=ok`。

## 4. 仓库发布

本地发布前执行：

```powershell
git fetch origin
git rev-list --left-right --count origin/master...master
.\.venv\Scripts\python.exe -m pytest
git diff --check
git push origin master
```

只有结果确认远程未领先、测试通过且无密钥/本地数据时才能推送。禁止强制推送。ECS 克隆后使用 `git rev-parse HEAD` 核对发布提交。

## 5. Ubuntu 22.04 smoke 准备

系统已具备 Git。先克隆已发布仓库，再以 root 预演并执行基础安装：

```bash
sudo git clone https://github.com/bbccoolly/football-cups.git /opt/football-cups
cd /opt/football-cups
sudo bash scripts/linux/bootstrap-smoke.sh
sudo bash scripts/linux/bootstrap-smoke.sh --apply
```

脚本完成系统升级、Python 3.11、2 GiB swap、服务用户和目录准备，但不会重启、修改仓库、安装 service 或启用 timer。若存在 `/var/run/reboot-required`，先重启并重新确认：

```bash
timedatectl
df -h
free -h
python3.11 --version
curl -I https://trade.500.com/jczq/
```

代码固定安装到 `/opt/football-cups`，由 root 管理；`football-cups` 用户无 sudo 权限且不能修改代码。环境文件放在 `/etc/football-cups/collector.env`，权限为 `0640 root:football-cups`，工作区 `.env` 只建立指向它的受控符号链接。真实密码、Token 和 AccessKey 不得进入命令历史、Git、日志或聊天。

基础环境重启后安装项目并锁定权限：

```bash
cd /opt/football-cups
sudo python3.11 -m venv .venv
sudo .venv/bin/python -m pip install --upgrade pip
sudo .venv/bin/python -m pip install -e '.[dev]'
sudo chown -R root:football-cups /opt/football-cups
sudo chmod -R g+rX,o-rwx /opt/football-cups
sudo install -o root -g football-cups -m 0640 /dev/null /etc/football-cups/collector.env
sudo ln -s /etc/football-cups/collector.env /opt/football-cups/.env
```

使用 `sudoedit /etc/football-cups/collector.env` 写入下列非密钥 smoke 配置，不要在聊天中粘贴未来的数据库密码或 OSS 凭据。

smoke 环境配置：

```text
APP_TIMEZONE=Asia/Shanghai
LOG_LEVEL=INFO
FOOTBALL_CUPS_DATA_DIR=/var/lib/football-cups-smoke/500
FOOTBALL_CUPS_REQUIRED_MOUNT=
COLLECTOR_DISK_WARNING_FREE_GB=10
COLLECTOR_DISK_CRITICAL_FREE_GB=5
COLLECTOR_DISK_WARNING_FREE_PERCENT=20
COLLECTOR_DISK_CRITICAL_FREE_PERCENT=10
COLLECTOR_HEALTH_HEARTBEAT_MAX_AGE_MINUTES=10
COLLECTOR_HEALTH_DISCOVERY_MAX_AGE_MINUTES=45
COLLECTOR_HEALTH_CLOCK_MAX_AGE_MINUTES=45
```

## 6. 隔离 smoke

以服务用户依次运行；pytest 的临时目录显式放入可写 smoke 路径：

```bash
sudo -u football-cups /opt/football-cups/.venv/bin/python -m pytest \
  --basetemp=/var/lib/football-cups-smoke/500/test-tmp -p no:cacheprovider
sudo -u football-cups /opt/football-cups/.venv/bin/football-cups-collector init --workspace /opt/football-cups
sudo -u football-cups /opt/football-cups/.venv/bin/football-cups-collector health --workspace /opt/football-cups
sudo -u football-cups /opt/football-cups/.venv/bin/football-cups-collector discover --workspace /opt/football-cups
sudo -u football-cups /opt/football-cups/.venv/bin/football-cups-collector smoke-live --workspace /opt/football-cups \
  --active-fixture-id <active-id> --completed-fixture-id <completed-id>
sudo -u football-cups /opt/football-cups/.venv/bin/football-cups-collector report-daily --workspace /opt/football-cups
```

活跃 fixture 从最新完整 DiscoveryRun 中选择；完场 fixture 必须仍能同时出现在完场页和分析页。通过条件：六页面全部成功、正则与 DOM 清单一致、三个核心市场成功、让球指数有明确分类、完场页和分析页均取得目标比分证据、所有请求均有 blob/manifest。仅访问竞彩首页不算完整 smoke。

## 7. 数据盘准备

在阿里云控制台增加至少 100 GB ESSD 后，先人工核对 `lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS,SERIAL`。禁止根据经验假设设备名。

安全脚本默认只读；实际格式化必须重复提供同一设备并显式 `--apply`：

```bash
sudo bash scripts/linux/prepare-data-disk.sh --device /dev/<confirmed-disk>
sudo bash scripts/linux/prepare-data-disk.sh --device /dev/<confirmed-disk> \
  --confirm-device /dev/<confirmed-disk> --apply
```

脚本拒绝已经挂载、含分区或已有文件系统签名的设备。完成后必须执行 `mount -a`、`findmnt /srv/football-cups`、写入测试和重启测试，确认 UUID 挂载稳定。

## 8. OSS 与恢复闭环

`backup-oss` 只生成本地内容寻址布局：

```text
objects/sha256/<first-two>/<sha256>
runs/<run-id>/manifest.json
runs/<run-id>/complete.json
```

该本地目录只是上传暂存区，不是异机备份。OSS 必须为杭州私有 Bucket，ECS 使用 RAM Role 和内网 Endpoint `https://oss-cn-hangzhou-internal.aliyuncs.com`。

完成 `ossutil` RAM Role 配置后，使用下列脚本预演和执行上传、全新下载及 SHA-256 恢复：

```bash
sudo -u football-cups bash scripts/linux/verify-oss-roundtrip.sh \
  --upload-root /srv/football-cups/backup-staging/oss-layout \
  --remote-uri oss://<bucket>/<prefix> --run-id <run-id> \
  --download-root /srv/football-cups/restore-test/download \
  --verify-target /srv/football-cups/restore-test/verified

# 所有路径和 RAM Role 检查正确后才增加 --apply
```

没有远端 `complete.json`、manifest 哈希不一致、对象缺失或恢复文件哈希不一致时，备份无效。

## 9. PostgreSQL 17

从 PGDG 安装 PostgreSQL 17，数据目录使用 `/srv/football-cups/postgresql/17-main`，仅监听 Unix socket 和 `127.0.0.1`。安全组和主机防火墙不得开放 5432。安装脚本默认只预演：

```bash
sudo bash scripts/linux/install-postgresql.sh
sudo bash scripts/linux/install-postgresql.sh --apply
```

脚本在安装包前关闭默认集群自动创建，避免 PostgreSQL 把 17/main 放到系统盘；发现已有集群位于其他目录时拒绝自动迁移。

2 vCPU / 4 GiB 默认参数：

```text
shared_buffers=512MB
effective_cache_size=2GB
work_mem=8MB
maintenance_work_mem=128MB
max_connections=20
```

数据库账号使用 `scram-sha-256`。应用账号和数据库通过交互命令创建：

```bash
sudo -u postgres createuser --pwprompt football_cups
sudo -u postgres createdb --owner=football_cups football_cups
sudo install -o football-cups -g football-cups -m 0600 /dev/null /var/lib/football-cups/.pgpass
sudo -u football-cups editor /var/lib/football-cups/.pgpass
```

pgpass 使用标准 `host:port:database:user:password` 格式，真实值不得粘贴到聊天。环境文件只写无密码的 `DATABASE_URL=postgresql://football_cups@127.0.0.1:5432/football_cups`。恢复文件事实后依次执行 `rebuild-state`、数据库迁移、首次导入、重复导入、独立空库重放和 as-of 越界审计。

## 10. systemd 与正式切换

安装脚本默认只安装和校验 unit，不启用 timer：

```bash
sudo bash scripts/linux/install-systemd.sh
sudo systemctl start football-cups-collector.service
sudo systemctl start football-cups-db-import.service
sudo systemctl status football-cups-collector.service football-cups-db-import.service
```

手工 service、`health=ok`、数据库、OSS 和数据盘门禁全部通过后才执行正式切换：

1. 保持 Windows 采集运行，先完成预同步。
2. 停止 Windows 采集任务并记录 UTC 时间。
3. 生成最后增量备份，上传并在 ECS 恢复验证。
4. 再次手工运行采集、导入和 health。
5. 执行 `sudo bash scripts/linux/install-systemd.sh --enable`。
6. 10 分钟内确认采集心跳和数据库导入成功。

正式切换后只保留一个采集写入者。云端 7 天和 30 天验收从切换时间重新计时，本地与云端运行时间不得拼接。

## 11. 正式切换门禁

- 本地精确 24 小时窗口报告和首日人工 3 场核验完成。
- 数据盘、2 GiB swap、NTP、系统更新和重启验证完成。
- 六页面、四市场、完场页和分析页 smoke 通过。
- `health=ok`，PostgreSQL 空库重放、重复导入和 as-of 审计通过。
- OSS 完成真实上传、全新下载和哈希恢复。
- `systemd-analyze verify` 无错误，服务用户不能修改代码和环境文件。
- 安全组只开放受限 SSH，不开放 PostgreSQL、Web 或管理端口。
- Windows 与 ECS 不存在两个正式采集写入者。

## 12. 回滚

任一条件触发回滚：心跳超过 10 分钟、完整发现超过 45 分钟、时钟偏差超过 30 秒、数据盘丢失、剩余空间低于严重阈值、OSS 恢复失败或核心市场连续三轮采集失败。

回滚时停止并禁用云端 timer，保留云端数据目录和日志，再恢复 Windows 单写入者。必须在 `docs/project-status.md` 记录云端停止时间、Windows 恢复时间、数据空窗、失败原因和新的唯一下一步。
