# Jelly Kids Guard｜Jellyfin 儿童观影守护

给小米看动画片：**集数 + 时长双上限、最低保障分钟、按星期设不同限额**，超限自动软锁（当前集播完，下一集打不开）。全可视化配置，不用手写配置文件；用户和媒体库自动从 Jellyfin 拉取。

## 规则语义（一句话）

```
lock = (已播集数 >= 当天集数上限 OR 已播分钟 >= 当天时长上限)
       AND 已播分钟 >= 最低保障分钟
```

- 集数上限 / 时长上限任一设为 `0` = 该项不限。
- 最低保障 `0` = 关闭保障（纯 OR 逻辑）。
- 例：上限 2 集 / 20 分钟、保障 20 分钟：
  - 10 分钟一集 → 播完 2 集（20 分钟）锁；
  - 5 分钟一集 → 2 集才 10 分钟，放行，满 20 分钟再锁（当前集播完）；
  - 21 分钟一集 → 播完这集就锁。

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
