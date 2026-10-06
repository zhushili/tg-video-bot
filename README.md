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
2. 按提示用手机给机器人发一条消息，在终端确认后自动设为管理员

再次运行同一命令即升级；命令末尾加 ` admin` 添加管理员，加 ` uninstall` 卸载。

## 使用

私聊直接发链接；`/audio 链接` 只下音频；`/dl 链接` 在群组里用；`/cancel` 取消。陌生人给机器人发消息时，管理员会收到「✅ 允许」按钮，点一下即可添加；也可用 `/allow ID`、`/remove ID`、`/users` 管理，`/status`、`/update` 查看状态和更新。

## 配置

`/opt/tgdl/.env`，修改后 `systemctl restart tgdl`，日志 `journalctl -u tgdl -f`。

- `API_ID` / `API_HASH`：2GB 模式，在 [my.telegram.org](https://my.telegram.org) → API development tools 申请
- `MAX_HEIGHT`：最高分辨率，默认 720；`ALLOWED_USERS=*`：允许所有人使用；`COOKIES_FILE`：YouTube 提示 "not a bot" 时使用（放在 `/opt/tgdl/data/` 并 `chown tgdl:tgdl`）
