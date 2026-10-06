# tg-video-bot

轻量 Telegram 视频下载机器人，专为 **1 核 / 512MB 内存 / 2GB 磁盘** 的 Ubuntu 24.04 小 VPS 设计。基于 yt-dlp，支持 YouTube、B 站、X、TikTok、Instagram 等上千个网站。

- 只需 Bot Token 即可使用，单文件上限 50MB，超出自动降画质；填上 API ID/Hash 上限提升到 2GB
- 优先下载 H.264 MP4，Telegram 可直接播放；不转码，下载进程约 80MB 内存
- 任务排队执行，按剩余磁盘自动限制文件大小，发完即删；yt-dlp 每天自动更新

## 安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/zhushili/tg-video-bot/main/install.sh)
```

1. 按提示填入 [@BotFather](https://t.me/BotFather) 给的 Token，API ID 没有就直接回车
2. 给机器人发 `/id`，把 ID 填到 `/opt/tgdl/.env` 的 `ADMIN_IDS=`，执行 `systemctl restart tgdl`

再次运行同一命令即升级；命令末尾加 ` uninstall` 即卸载。

## 使用

私聊直接发链接；`/audio 链接` 只下音频；`/dl 链接` 在群组里用；`/cancel` 取消；管理员可用 `/status`、`/update`。

## 配置

`/opt/tgdl/.env`，修改后 `systemctl restart tgdl`，日志 `journalctl -u tgdl -f`。

- `API_ID` / `API_HASH`：2GB 模式，在 [my.telegram.org](https://my.telegram.org) → API development tools 申请
- `ALLOWED_USERS`：允许使用的用户 ID（逗号分隔，`*` 为所有人）；`MAX_HEIGHT`：最高分辨率，默认 720
- `COOKIES_FILE`：YouTube 提示 "not a bot" 时使用，文件放在 `/opt/tgdl/data/` 并 `chown tgdl:tgdl`
