#!/usr/bin/env python3
"""yt-dlp 下载子进程。

每个任务单独起一个进程运行，结束后内存全部归还系统，崩溃也不会拖垮机器人。

用法: worker.py <url> <输出目录> <最大字节数> <video|audio> [yt-dlp 命令行参数...]
通过 stdout 输出以 "@@TGDL " 开头的 JSON 行与主进程通信:
  {"t": "info", ...}      解析完成
  {"t": "progress", ...}  下载进度
  {"t": "done", ...}      下载完成
  {"t": "error", "msg"}   失败
"""
import json
import os
import re
import sys
import time

import yt_dlp
from yt_dlp.utils import DownloadError

MARK = '@@TGDL '
MB = 1024 * 1024
ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
SKIP_EXT = ('.part', '.ytdl', '.json', '.jpg', '.jpeg', '.png', '.webp', '.temp')
# 文件太大时依次降级尝试
VIDEO_STEPS = (1080, 720, 480, 360, 240, 144)
AUDIO_STEPS = (128, 96, 64, 48)


class TooLarge(Exception):
    pass


class UserError(Exception):
    pass


def emit(**kw):
    sys.stdout.write(MARK + json.dumps(kw, ensure_ascii=False) + '\n')
    sys.stdout.flush()


class Logger:
    """把 yt-dlp 的普通输出丢掉，只把警告和错误写到 stderr（主进程会记录日志）。"""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        print(ANSI_RE.sub('', msg), file=sys.stderr, flush=True)

    def error(self, msg):
        print(ANSI_RE.sub('', msg), file=sys.stderr, flush=True)


def estimate_size(fmt):
    total = 0
    for f in fmt.get('requested_formats') or [fmt]:
        size = f.get('filesize') or f.get('filesize_approx')
        if not size:
            return 0  # 有任何一部分大小未知就当作未知
        total += size
    return int(total)


def size_aware_selector(ydl, spec, mode, max_bytes):
    """在原格式选择的基础上，挑选能放进 max_bytes 的最高画质。

    依次尝试：原始选择 → 1080p → 720p → … → 144p（音频则按码率降级），
    返回第一个已知大小且不超限的格式。都不知道大小时用最高画质，由下载过程中的检查兜底。
    """
    if mode == 'audio':
        variants = [spec] + [f'({spec})[abr<=?{a}]' for a in AUDIO_STEPS]
    else:
        variants = [spec] + [f'({spec})[height<=?{h}]' for h in VIDEO_STEPS]
    selectors = [ydl.build_format_selector(v) for v in variants]

    def select(ctx):
        unknown = smallest = None
        for sel in selectors:
            picked = next(iter(sel(ctx)), None)
            if picked is None:
                continue
            size = estimate_size(picked)
            if not size:
                unknown = unknown or picked
            elif size <= max_bytes:
                yield picked
                return
            elif smallest is None or size < estimate_size(smallest):
                smallest = picked
        if unknown or smallest:
            yield unknown or smallest  # 超限的会在下载前被拒绝并提示大小

    return select


def find_output(info, outdir):
    for d in info.get('requested_downloads') or []:
        path = d.get('filepath')
        if path and os.path.isfile(path):
            return path
    # 兜底：取目录里最大的媒体文件
    files = [
        os.path.join(outdir, n) for n in os.listdir(outdir)
        if not n.lower().endswith(SKIP_EXT)
    ]
    files = [f for f in files if os.path.isfile(f)]
    return max(files, key=os.path.getsize) if files else None


def main():
    url, outdir, max_bytes, mode = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    opts = yt_dlp.parse_options(sys.argv[5:]).ydl_opts
    spec = opts.get('format') or 'bv*+ba/b'

    done_parts = {}  # 已完成的分段（视频/音频分开下载时）
    last_emit = [0.0]
    too_large = [False]  # yt-dlp 可能把 hook 里抛出的异常包成 DownloadError，用标志位判断

    def hook(d):
        name = d.get('filename') or ''
        if d['status'] == 'finished':
            done_parts[name] = d.get('total_bytes') or d.get('downloaded_bytes') or 0
            return
        if d['status'] != 'downloading':
            return
        finished = sum(v for k, v in done_parts.items() if k != name)
        got = d.get('downloaded_bytes') or 0
        exact_total = d.get('total_bytes') or 0
        # 已知大小直接超限，或者实际下载量已经超限，立即中止，防止撑爆磁盘
        if max_bytes and (finished + max(got, exact_total) > max_bytes * 1.02):
            too_large[0] = True
            raise TooLarge()
        now = time.monotonic()
        if now - last_emit[0] < 2:
            return
        last_emit[0] = now
        is_audio = (d.get('info_dict') or {}).get('vcodec') == 'none'
        emit(
            t='progress',
            part='音频' if is_audio else '视频',
            got=got,
            total=exact_total or d.get('total_bytes_estimate') or 0,
            speed=d.get('speed') or 0,
            eta=d.get('eta'),
        )

    opts.update(
        quiet=True,
        no_warnings=False,
        noprogress=True,
        ignoreerrors=False,
        logger=Logger(),
        progress_hooks=[hook],
        paths={'home': outdir, 'temp': outdir},
    )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.format_selector = size_aware_selector(ydl, spec, mode, max_bytes)
            info = ydl.extract_info(url, download=False)
            if not info:
                raise UserError('解析失败，没有拿到视频信息')
            if info.get('_type') == 'playlist' or 'entries' in info:
                entries = [e for e in (info.get('entries') or []) if e]
                if not entries:
                    raise UserError('这个链接里没有找到可下载的视频')
                info = entries[0]
            if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming'):
                raise UserError('不支持下载直播')

            est = estimate_size(info)
            if max_bytes and est > max_bytes:
                raise UserError(
                    f'最低画质也有约 {est / MB:.1f} MB，超过上限 {max_bytes / MB:.1f} MB'
                )
            emit(t='info', title=info.get('title') or '', est=est, height=info.get('height') or 0)

            info = ydl.process_ie_result(info, download=True)
            path = find_output(info, outdir)
            if not path:
                raise UserError('下载完成但没有找到文件')
            emit(
                t='done',
                file=path,
                title=info.get('title') or '',
                uploader=info.get('uploader') or info.get('channel') or '',
                duration=info.get('duration') or 0,
                width=info.get('width') or 0,
                height=info.get('height') or 0,
                url=info.get('webpage_url') or url,
            )
    except UserError as e:
        emit(t='error', msg=str(e))
        sys.exit(2)
    except Exception as e:  # noqa: BLE001  任何异常都要告诉主进程原因
        if too_large[0]:
            emit(t='error', msg=f'文件超过上限 {max_bytes / MB:.1f} MB，已中止')
            sys.exit(2)
        if isinstance(e, DownloadError):
            msg = ANSI_RE.sub('', str(e)).removeprefix('ERROR: ').strip()
        else:
            msg = f'{type(e).__name__}: {e}'
        emit(t='error', msg=msg[:500] or '下载失败')
        sys.exit(1)


if __name__ == '__main__':
    main()
