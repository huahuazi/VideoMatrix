"""结构混剪的 ffmpeg 命令构造。

参数模板还原自 2.8.4.1 的 structured_service.py 字节码常量:

  * 统一 48000 Hz / AAC / 192k,画面 libx264 veryfast / crf 20 / yuv420p
  * 用 trim=start_pts / end_pts 做精确切点(半开区间 PTS),
    避免 45.766667 秒这类小数被直接比较时漏掉下一镜的第一帧
  * 所有片段先 scale+pad 到同一画布(force_divisible_by=2 保证偶数),
    再 concat,最后统一 fps 与 setpts=N 得到连续时基
  * 无音轨素材补 anullsrc 静音床,再与原声 amix
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITRATE = "192k"
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "veryfast"
VIDEO_CRF = "20"
PIX_FMT = "yuv420p"


def ffmpeg_binary() -> str:
    """优先用打包内置的 ffmpeg,其次系统 PATH。"""
    candidate = Path(__file__).resolve().parent / "ffmpeg"
    if candidate.is_file():
        return str(candidate)
    return "ffmpeg"


def probe_binary() -> str:
    candidate = Path(__file__).resolve().parent / "ffprobe"
    if candidate.is_file():
        return str(candidate)
    return "ffprobe"


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

    if keep_audio and has_audio:
        audio = (
            "[{i}:a:0]{trim},asetpts=PTS-STARTPTS,"
            "aresample={rate}:first_pts=0,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
        ).format(i=index, trim=trim, rate=AUDIO_RATE)
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

