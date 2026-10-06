#!/usr/bin/env bash
# Telegram 视频下载机器人 一键安装（Ubuntu 24.04，amd64 / arm64）
#
#   安装 / 升级：bash <(curl -fsSL https://raw.githubusercontent.com/zhushili/tg-video-bot/main/install.sh)
#   卸载：      bash <(curl -fsSL https://raw.githubusercontent.com/zhushili/tg-video-bot/main/install.sh) uninstall
#
# 重复执行即为升级，不会覆盖已有的 .env。
# 免交互安装可预先设置环境变量：BOT_TOKEN  API_ID  API_HASH  ADMIN_IDS
# 其他可选：SWAP_MB=256（0 = 不创建 swap）  INSTALL_FFMPEG=1  INSTALL_QJS=1
set -euo pipefail

REPO="${TGDL_REPO:-zhushili/tg-video-bot}"
BRANCH="${TGDL_BRANCH:-main}"
APP_DIR=/opt/tgdl
SERVICE=tgdl
ENV_FILE="$APP_DIR/.env"
FILES="bot.py worker.py requirements.txt .env.example"
SWAP_MB="${SWAP_MB:-256}"
INSTALL_FFMPEG="${INSTALL_FFMPEG:-1}"
INSTALL_QJS="${INSTALL_QJS:-1}"

info() { echo -e "\033[32m==>\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
die()  { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }
free_mb() { df -Pm "$1" | awk 'NR==2{print $4}'; }
has_tty() { (exec </dev/tty) 2>/dev/null; }
ask() { local v=''; read -rp "$1" v </dev/tty || true; printf '%s' "$v"; }
set_env() {
  local key="$1" val="${2//[$'\t\r\n ']/}"
  sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
}
add_env_id() {
  local key="$1" id="$2" cur
  cur=$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true)
  case ",$cur," in *",$id,"*) return 0 ;; esac
  if grep -qE "^${key}=" "$ENV_FILE"; then set_env "$key" "${cur:+$cur,}$id"; else echo "${key}=$id" >> "$ENV_FILE"; fi
}

# 让主人用手机给机器人发一条消息，自动识别 ID（不用自己去查），识别到后在终端确认
detect_admin() {
  local token
  token=$(grep -E '^BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
  [ -n "$token" ] && has_tty || return 0
  systemctl stop "$SERVICE" 2>/dev/null || true  # 机器人在运行的话会抢走消息
  python3 - "$token" <<'PY' || true
import json, sys, time, urllib.request
base = f'https://api.telegram.org/bot{sys.argv[1]}/'
tty = open('/dev/tty', 'r+')

def say(s, end='\n'):
    tty.write(s + end)
    tty.flush()

def api(method, http_timeout=40, **params):
    req = urllib.request.Request(base + method, json.dumps(params).encode(), {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=http_timeout) as r:
        return json.load(r)['result']

try:
    me = api('getMe')
    api('deleteWebhook')
except Exception as e:
    say(f'[!] 连接 Telegram 失败（{e}），跳过。之后给机器人发 /id 查看 ID')
    sys.exit(0)
say(f'\n\033[32m==>\033[0m 设置管理员：用手机打开 https://t.me/{me["username"]} 点「开始」或发任意消息')
say('    正在等待（最多 3 分钟，按 Ctrl+C 跳过）…')
offset, deadline = 0, time.time() + 180
try:
    while time.time() < deadline:
        try:
            updates = api('getUpdates', offset=offset, timeout=25, allowed_updates=['message'])
        except Exception:
            time.sleep(3)
            continue
        for u in updates:
            offset = u['update_id'] + 1
            m = u.get('message') or {}
            f = m.get('from')
            if not f or (m.get('chat') or {}).get('type') != 'private':
                continue
            name = ' '.join(x for x in (f.get('first_name'), f.get('last_name')) if x)
            if f.get('username'):
                name += f' (@{f["username"]})'
            say(f'    收到 {name} 的消息，ID：{f["id"]}。设为管理员？[Y/n] ', end='')
            if tty.readline().strip().lower() in ('', 'y', 'yes'):
                api('getUpdates', offset=offset, timeout=0)  # 标记已读，机器人启动后不会重复处理
                try:
                    api('sendMessage', chat_id=f['id'], text='✅ 你已被设为管理员，直接发视频链接就能下载。')
                except Exception:
                    pass
                print(f['id'])
                sys.exit(0)
    say('[!] 3 分钟内没收到消息，跳过。之后可以重新运行安装命令并在末尾加 admin')
except KeyboardInterrupt:
    say('\n    已跳过')
PY
}

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（先执行 sudo -i）"

# ---------------------------------------------------------------- 卸载
if [ "${1:-}" = "uninstall" ]; then
  systemctl disable --now "$SERVICE" "$SERVICE-update.timer" 2>/dev/null || true
  rm -f /etc/systemd/system/$SERVICE.service /etc/systemd/system/$SERVICE-update.{service,timer}
  systemctl daemon-reload
  rm -rf "$APP_DIR"
  if id tgdl &>/dev/null; then userdel tgdl; fi
  info "已卸载（/swapfile 保留，不需要可以 swapoff /swapfile 后删除并去掉 /etc/fstab 里那一行）"
  exit 0
fi

# ---------------------------------------------------------------- 添加管理员
if [ "${1:-}" = "admin" ]; then
  [ -f "$ENV_FILE" ] || die "还没有安装，先运行一键安装命令（末尾不加 admin）"
  has_tty || die "需要在交互式终端里运行"
  new_id=$(detect_admin)
  if [ -n "$new_id" ]; then
    add_env_id ADMIN_IDS "$new_id"
    chown tgdl:tgdl "$ENV_FILE" && chmod 600 "$ENV_FILE"
  fi
  systemctl restart "$SERVICE"
  if [ -n "$new_id" ]; then info "已把 $new_id 设为管理员，机器人已重启 ✅"; fi
  exit 0
fi

case "$(dpkg --print-architecture)" in
  amd64) FF_ARCH=amd64; QJS_ARCH=x86_64 ;;
  arm64) FF_ARCH=arm64; QJS_ARCH=aarch64 ;;
  *) die "不支持的架构：$(dpkg --print-architecture)" ;;
esac

MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
info "磁盘剩余 $(free_mb /) MB，内存 ${MEM_MB} MB"
[ "$(free_mb /)" -ge 400 ] || die "磁盘剩余不足 400MB，先清理：apt-get clean; journalctl --vacuum-size=20M"

# ---------------------------------------------------------------- 系统依赖
info "安装系统依赖（python3-venv、curl、xz-utils）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv ca-certificates curl xz-utils >/dev/null
apt-get clean

# ---------------------------------------------------------------- 获取程序文件
# 和 install.sh 放在同一目录就用本地文件，否则（curl 一键安装）从 GitHub 下载
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/bot.py" ] || [ ! -f "$SRC_DIR/worker.py" ]; then
  SRC_DIR=$(mktemp -d)
  trap 'rm -rf "$SRC_DIR"' EXIT
  info "从 GitHub 下载程序（$REPO@$BRANCH）"
  for f in $FILES; do
    curl -fsSL --retry 3 -o "$SRC_DIR/$f" "https://raw.githubusercontent.com/$REPO/$BRANCH/$f" \
      || die "下载 $f 失败，检查 VPS 能否访问 raw.githubusercontent.com"
  done
fi

# ---------------------------------------------------------------- swap
if [ "$SWAP_MB" -gt 0 ] && [ -z "$(swapon --noheadings --show 2>/dev/null)" ]; then
  if [ "$(free_mb /)" -gt $((SWAP_MB + 700)) ]; then
    info "创建 ${SWAP_MB}MB swap（内存小，防止下载时被 OOM 杀掉）"
    if { fallocate -l "${SWAP_MB}M" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_MB" status=none; } \
       && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile 2>/dev/null; then
      grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    else
      rm -f /swapfile
      warn "这台 VPS 不支持 swap（OpenVZ/LXC 常见），已跳过"
    fi
  else
    warn "磁盘空间紧张，跳过创建 swap"
  fi
fi

# ---------------------------------------------------------------- 安装程序
info "安装程序到 $APP_DIR"
id tgdl &>/dev/null || useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin tgdl
mkdir -p "$APP_DIR/bin" "$APP_DIR/data"
for f in $FILES; do install -m 644 "$SRC_DIR/$f" "$APP_DIR/$f"; done

info "安装 Python 依赖（1 核机器大约 1~3 分钟）"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
"$APP_DIR/venv/bin/pip" install -q -U -r "$APP_DIR/requirements.txt"

# ffmpeg：合并音视频用。静态版只保留 ffmpeg 本体约 80MB（apt 版要 300MB+）
if [ "$INSTALL_FFMPEG" = 1 ] && [ ! -x "$APP_DIR/bin/ffmpeg" ] && ! command -v ffmpeg >/dev/null; then
  info "下载静态版 ffmpeg（约 40MB）"
  tmp=$(mktemp -d "$APP_DIR/tmp.XXXXXX")
  if curl -fL --retry 3 --progress-bar -o "$tmp/ff.tar.xz" \
       "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FF_ARCH}-static.tar.xz" \
     && tar -xJf "$tmp/ff.tar.xz" -C "$tmp" --wildcards '*/ffmpeg'; then
    mv "$tmp"/ffmpeg-*-static/ffmpeg "$APP_DIR/bin/ffmpeg"
    chmod 755 "$APP_DIR/bin/ffmpeg"
  else
    warn "ffmpeg 下载失败：仍可运行，但只能下载音视频一体的格式（YouTube 最高约 360p）"
  fi
  rm -rf "$tmp"
fi

# QuickJS-NG：YouTube 解析需要 JS 引擎，约 2.5MB（Deno 要 100MB+）
if [ "$INSTALL_QJS" = 1 ]; then
  info "下载 QuickJS-NG"
  if curl -fsSL --retry 3 -o "$APP_DIR/bin/qjs.new" \
       "https://github.com/quickjs-ng/quickjs/releases/latest/download/qjs-linux-${QJS_ARCH}" \
     && chmod 755 "$APP_DIR/bin/qjs.new" && "$APP_DIR/bin/qjs.new" -e '0'; then
    mv -f "$APP_DIR/bin/qjs.new" "$APP_DIR/bin/qjs"
  else
    rm -f "$APP_DIR/bin/qjs.new"
    warn "QuickJS 下载失败：YouTube 可能只能下载到部分格式"
  fi
fi

# ---------------------------------------------------------------- 配置
NEW_ENV=0
if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
  NEW_ENV=1
fi
for key in BOT_TOKEN API_ID API_HASH ADMIN_IDS; do
  if [ -n "${!key:-}" ]; then set_env "$key" "${!key}"; fi
done
if [ "$NEW_ENV" = 1 ] && has_tty; then
  echo
  [ -n "${BOT_TOKEN:-}" ] || set_env BOT_TOKEN "$(ask 'BOT_TOKEN（@BotFather 给的）: ')"
  if [ -z "${API_ID:-}" ]; then
    echo "API_ID / API_HASH 可选：不填 = 单文件上限 50MB；填了 = 上限 2GB（在 my.telegram.org 申请）"
    v=$(ask 'API_ID（没有就直接回车）: ')
    set_env API_ID "$v"
    [ -z "$v" ] || set_env API_HASH "$(ask 'API_HASH: ')"
  fi
fi
if ! grep -qE '^ADMIN_IDS=[0-9-]' "$ENV_FILE"; then
  new_id=$(detect_admin)
  [ -z "$new_id" ] || add_env_id ADMIN_IDS "$new_id"
fi
chown -R tgdl:tgdl "$APP_DIR"
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------- systemd
MEM_MAX=$((MEM_MB * 8 / 10))
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Telegram video downloader bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tgdl
Group=tgdl
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$APP_DIR/bin:$APP_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=10
Nice=5
MemoryMax=${MEM_MAX}M
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

# 每天自动更新 yt-dlp（网站经常改版，旧版本很快就会失效）
cat > /etc/systemd/system/$SERVICE-update.service <<EOF
[Unit]
Description=Update yt-dlp for $SERVICE

[Service]
Type=oneshot
User=tgdl
Nice=10
Environment=PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ExecStart=$APP_DIR/venv/bin/pip install -q -U "yt-dlp[default]"
EOF

cat > /etc/systemd/system/$SERVICE-update.timer <<EOF
[Unit]
Description=Daily yt-dlp update for $SERVICE

[Timer]
OnCalendar=daily
RandomizedDelaySec=2h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE-update.timer" >/dev/null 2>&1

echo
if grep -qE '^BOT_TOKEN=.+' "$ENV_FILE"; then
  systemctl enable "$SERVICE" >/dev/null 2>&1
  systemctl restart "$SERVICE"
  sleep 8
  if systemctl is-active --quiet "$SERVICE"; then
    info "机器人已启动 ✅"
  else
    warn "启动失败，查看日志：journalctl -u $SERVICE -n 50 --no-pager"
  fi
else
  warn "还没填 BOT_TOKEN。编辑 $ENV_FILE 后执行：systemctl enable --now $SERVICE"
fi

info "安装目录占用 $(du -sh "$APP_DIR" | cut -f1)，磁盘剩余 $(free_mb /) MB"
if ! grep -qE '^ADMIN_IDS=[0-9-]' "$ENV_FILE"; then
  cat <<EOF

还没有管理员，运行下面的命令并按提示给机器人发一条消息即可：
  bash <(curl -fsSL https://raw.githubusercontent.com/$REPO/$BRANCH/install.sh) admin
EOF
fi
echo "日志：journalctl -u $SERVICE -f    配置：$ENV_FILE"
