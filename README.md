# Jelly Kids Guard｜Jellyfin 儿童观影守护

给孩子看动画片：**集数 + 时长双上限、最低保障分钟、按星期设不同限额**，超限自动软锁（当前集播完，下一集打不开）。全可视化配置，不用手写配置文件；用户和媒体库自动从 Jellyfin 拉取。

## 规则语义（一句话）

```
集数线：已播集数 >= 当天集数上限 且 已播分钟 >= A  -> 锁
时长线：已播分钟 >= B（不管集数，如单集 45 分钟播完即锁） -> 锁
下集门控 C（只在当天已看过内容后生效）：
  已播分钟 + 预估下集分钟 > C  -> 现在锁（当前集播完，下集打不开）
  否则放行下集，播完再按上面两条结算
```

- A = 最低保障分钟，B = 软上限，C = 硬上限，要求 A≤B≤C；任一项设 `0` = 关闭该项。
- 锁都是软锁：收回媒体库，当前正在播的不掐断。
- 预估下集时长优先级：真实下一集（顺序连播时经 NextUp 拿到）> 本剧平均 > 今日平均 > 服务器页的兜底值。随机点播拿不到真实下集时自动降级。
- 例（上限 2 集，A=20，B=40，C=60）：第 1 集 35 分钟（<B，可播），下集预估 35 → 35+35=70>C → 本集播完即锁；下集预估 20 → 55≤C → 放行，播完后 55≥B 锁。

## 每天看几次 + 间隔（周末早上一次、下午一次）

每天规则里还有两格：**次数**（当天允许看几次，0=不限）、**间隔**（两次之间至少隔几小时，可小数如 0.5）。

- 两次播放记录相隔 30 分钟以上算另一次。
- 上次播完、间隔没到 → 锁定并显示“冷却至 HH:MM”；时间一到下次轮询自动解锁，不用手动。
- 次数用完 → 当天锁定。次数和 A/B/C 是“或”关系，任一触发都锁。
- A/B/C 仍按**全天累计**结算（上午用了 15 分钟，下午剩 B−15）。

## 前置条件

1. Jellyfin（linuxserver 镜像即可）插件中心安装 **Playback Reporting**，设置默认。
   - Jellyfin 10.11 配插件 v17+，Jellyfin 12.x 配插件 v19。
2. Jellyfin 控制台 → API Keys，新建一个管理员 Key。

## 免 SSH 安装（绿联 NAS）

镜像由 GitHub Actions 自动构建到 `ghcr.io/endif12/jelly-kids-guard:latest`（push 到 main 即构建，多架构 amd64/arm64）：

1. NAS 先建好目录，如 `共享文件夹/docker/jellyguard/app_config`。
2. Docker 管理 → 项目 → 创建项目 → 把 `docker-compose.yml` 内容粘贴进去，
   把 `volumes` 左侧改成你的目录（如 `/volume1/docker/jellyguard/app_config:/app/data`），端口可改。
3. 启动后打开 `http://NAS_IP:35325`：
   - 「服务器」页填 Jellyfin 地址 + API Key → 保存并同步 → 测试连接。
   - 「规则设置」页选用户 → 设最低保障、白名单媒体库、周一到周日每天的上限 → 保存。
4. 受控用户不要勾 Jellyfin 的“允许访问所有媒体库”，要逐个勾选，否则软锁无处下手。

也支持 webhook：`GET /trigger`（全部）、`GET /trigger/{userId}`（单个）、状态 `GET /api/status`。

## 本地开发

```sh
pip install -r requirements.txt
set DATA_DIR=data  # Windows: set；Linux/macOS: export
python app/main.py
```

## 数据

全部存在挂载目录：`settings.json`（可视化配置）、`folders.bck`（锁之前的媒体库备份，用于恢复）。
