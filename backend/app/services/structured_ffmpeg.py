"""结构混剪的 ffmpeg 命令构造与执行。

参数模板还原自 2.8.4.1 的 structured_service.py 字节码常量:

  * 统一 48000 Hz / AAC / 192k,画面 libx264 veryfast / crf 20 / yuv420p
  * 用 trim=start_pts / end_pts 做精确切点(半开区间 PTS),
    避免 45.766667 秒这类小数被直接比较时漏掉下一镜的第一帧
  * 所有片段先 scale+pad 到同一画布(force_divisible_by=2 保证偶数),
    再 concat,最后统一 fps 与 setpts=N 得到连续时基
  * 无音轨素材补 anullsrc 静音床,再与原声 amix

工具发现与进程执行复用 app/core/ffmpeg.py,不另造一套:
`_find_tool` 会处理 PyInstaller 的 _MEIPASS 与打包目录,
`run_process` 已经做了 stderr 排空、取消与超时。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..core.ffmpeg import FFMPEG, FFPROBE, run_process

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITRATE = "192k"
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "veryfast"
VIDEO_CRF = "20"
PIX_FMT = "yuv420p"


def ffmpeg_binary() -> str:
    """打包内置优先,其次 PATH —— 由 app/core/ffmpeg.py 统一解析。"""
    return FFMPEG


def probe_binary() -> str:
    return FFPROBE


def _trim_expr(start: float, end: float, time_base: Optional[float] = None) -> str:
    """构造精确裁剪表达式。

    有素材时间基时用 tick 精度(半开区间),否则退化为浮点秒。
    """
    if time_base and time_base > 0:
        start_ticks = int(round(start / time_base))
        end_ticks = int(round(end / time_base)) - 1
        return "trim=start_pts=%d:end_pts=%d" % (start_ticks, end_ticks)
    return "trim=start=%.9f:end=%.9f" % (start, end)


def build_segment_chain(index: int, width: int, height: int, fps: int,
                        keep_audio: bool, has_audio: bool,
                        start: float, end: float,
                        time_base: Optional[float] = None) -> Dict[str, str]:
    """单个片段的 filter 片段与 map 目标。"""
    trim = _trim_expr(start, end, time_base)
    duration = max(0.0, end - start)
    frames = max(1, int(round(duration * fps)))

    video = (
        "[{i}:v:0]{trim},setpts=PTS-STARTPTS,"
        "fps={fps}:start_time=0:round=near:eof_action=pass,"
        "scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        "format={pix},trim=end_frame={frames},"
        "settb=expr=1/{fps},setpts=N[v{i}]"
    ).format(i=index, trim=trim, fps=fps, w=width, h=height,
             pix=PIX_FMT, frames=frames)

    # 目标采样数:与视频帧数对齐,保证音画等长 —— 这是 concat 的前提。
    target_samples = max(1, int(round(frames * AUDIO_RATE / float(fps))))

    if keep_audio and has_audio:
        # 音频不能用视频的 trim(那是按 PTS 切的视频滤镜,拿到音频流会类型不匹配)。
        # 用时间形式的 atrim(内部按流时基换算),再 resample 到 48kHz;
        # 末尾 apad + atrim=end_sample 把采样数精确对齐到 target_samples,
        # 否则 AAC 编码器可能在上游 EOF 前拿不到任何包(Invalid argument)。
        audio = (
            "[{i}:a:0]atrim=start={s:.9f}:end={e:.9f},"
            "asetpts=PTS-STARTPTS,"
            "aresample={rate}:first_pts=0,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            "apad=pad_dur=0.1,"
            "atrim=end_sample={n},"
            "asetpts=N/SR/TB[a{i}]"
        ).format(i=index, s=start, e=end, rate=AUDIO_RATE, n=target_samples)
    else:
        samples = int(round(duration * AUDIO_RATE))
        audio = (
            "anullsrc=r={rate}:cl=stereo,atrim=end_sample={n},"
            "asetpts=N/SR/TB[a{i}]"
        ).format(rate=AUDIO_RATE, n=max(1, samples), i=index)

    return {"video": video, "audio": audio}


def build_concat_command(segments: Sequence[Dict[str, Any]], output: Path,
                         width: int, height: int, fps: int,
                         keep_audio: bool = True) -> List[str]:
    """把多片段拼成一条成片。

    segments 每项: {path, start, end, has_audio?, time_base?}
    """
    if not segments:
        raise ValueError("没有可拼接的片段")

    chains: List[str] = []
    total_frames = 0
    total_samples = 0

    for index, segment in enumerate(segments):
        duration = max(0.0, float(segment["end"]) - float(segment["start"]))
        total_frames += max(1, int(round(duration * fps)))
        total_samples += max(1, int(round(duration * AUDIO_RATE)))
        parts = build_segment_chain(
            index, width, height, fps,
            keep_audio=keep_audio,
            has_audio=bool(segment.get("has_audio", True)),
            start=float(segment["start"]),
            end=float(segment["end"]),
            time_base=segment.get("time_base"),
        )
        chains.append(parts["video"])
        chains.append(parts["audio"])

    count = len(segments)
    pairs = "".join("[v%d][a%d]" % (i, i) for i in range(count))
    chains.append(
        "{pairs}concat=n={n}:v=1:a=1[joinedvideo][joinedaudio]".format(
            pairs=pairs, n=count)
    )
    chains.append(
        "[joinedvideo]fps={fps}:start_time=0:round=near:eof_action=pass,"
        "settb=expr=1/{fps},setpts=N[video]".format(fps=fps)
    )
    chains.append(
        "[joinedaudio]atrim=end_sample={n},asetpts=N/SR/TB[audio]".format(
            n=total_samples)
    )

    command = [
        ffmpeg_binary(),
        "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-copyts",
    ]
    for segment in segments:
        command.extend(["-ss", "%.9f" % float(segment["start"])])
        command.extend(["-threads", "2", "-i", str(segment["path"])])

    command.extend(["-filter_complex_threads", "1"])
    command.extend(["-filter_complex", ";\n".join(chains)])
    command.extend(["-map", "[video]"])
    if keep_audio:
        command.extend(["-map", "[audio]"])
    command.extend(["-frames:v", str(total_frames)])
    command.extend(["-fps_mode", "cfr"])
    command.extend(["-c:v", VIDEO_CODEC])
    command.extend(["-preset", VIDEO_PRESET])
    command.extend(["-crf", VIDEO_CRF])
    command.extend(["-pix_fmt", PIX_FMT])
    if keep_audio:
        command.extend(["-c:a", "aac"])
        command.extend(["-ar", str(AUDIO_RATE)])
        command.extend(["-ac", str(AUDIO_CHANNELS)])
        command.extend(["-b:a", AUDIO_BITRATE])
    else:
        command.extend(["-an"])
    command.extend(["-movflags", "+faststart"])
    command.append(str(output))
    return command


def probe_media(path: Path) -> Dict[str, Any]:
    """用 ffprobe 读时长/尺寸/音轨/时间基。失败返回空 dict。"""
    command = [
        probe_binary(), "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,r_frame_rate,time_base",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    import json
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}

    info: Dict[str, Any] = {"has_audio": False, "duration": None,
                            "width": None, "height": None, "time_base": None}
    try:
        info["duration"] = float((payload.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        pass
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "audio":
            info["has_audio"] = True
        elif stream.get("codec_type") == "video":
            info["width"] = stream.get("width")
            info["height"] = stream.get("height")
            base = str(stream.get("time_base") or "")
            if "/" in base:
                num, _, den = base.partition("/")
                try:
                    if float(den):
                        info["time_base"] = float(num) / float(den)
                except (TypeError, ValueError):
                    pass
    return info

def render_concat(segments: Sequence[Dict[str, Any]], output: Path,
                  width: int, height: int, fps: int,
                  keep_audio: bool = True,
                  is_cancelled: Optional[Callable[[], bool]] = None,
                  on_process: Optional[Callable[[Optional[subprocess.Popen]], None]] = None,
                  timeout: Optional[float] = None,
                  cwd: Optional[str] = None) -> Dict[str, Any]:
    """真正执行一次结构拼接。

    返回 {"ok": bool, "command": [...], "error": str|None, "output": str}
    - 输出目录不存在会自动创建
    - 目标已存在直接失败,不覆盖(与原版一致)
    - 取消 / 超时由 run_process 处理,ffmpeg 会被 terminate 后 kill
    """
    output = Path(output)
    if output.exists():
        return {"ok": False, "error": "目标文件已存在，拒绝覆盖",
                "output": str(output), "command": []}

    segments = [s for s in segments if s.get("path")]
    if not segments:
        return {"ok": False, "error": "没有可拼接的片段",
                "output": str(output), "command": []}

    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_concat_command(segments, output, width, height, fps, keep_audio)

    ok, error = run_process(command, is_cancelled=is_cancelled,
                            on_process=on_process, timeout=timeout, cwd=cwd)
    if ok and not output.is_file():
        return {"ok": False, "error": "FFmpeg 返回成功但未生成文件",
                "output": str(output), "command": command}
    return {"ok": bool(ok), "error": error, "output": str(output),
            "command": command}


def verify_output(path: Path, expected_duration: float,
                  width: int, height: int, fps: int,
                  tolerance: float = 0.35) -> Dict[str, Any]:
    """校验导出结果。

    原版会验证音轨格式、帧率、时长、帧数与非等间隔帧;这里做核心四项:
    文件存在、有视频轨、尺寸匹配、时长与预期吻合(AAC 音轨、偶发浮点误差留容差)。
    """
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return {"ok": False, "reason": "文件不存在或为空"}

    info = probe_media(path)
    if not info.get("duration"):
        return {"ok": False, "reason": "文件无法探测"}
    if not info.get("width"):
        return {"ok": False, "reason": "缺少视频轨道"}

    if (info.get("width"), info.get("height")) != (width, height):
        return {"ok": False,
                "reason": "尺寸不符: %sx%s, 预期 %sx%s"
                          % (info.get("width"), info.get("height"), width, height)}

    actual = float(info["duration"])
    if abs(actual - expected_duration) > max(tolerance, expected_duration * 0.05):
        return {"ok": False,
                "reason": "时长 %.3f 秒, 预期 %.3f 秒" % (actual, expected_duration)}

    return {"ok": True, "duration": actual,
            "width": info.get("width"), "height": info.get("height"),
            "has_audio": bool(info.get("has_audio"))}

