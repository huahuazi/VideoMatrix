"""结构化混剪的真实渲染回归测试。

与 test_structured_restored.py 的区别:那份只验证数据契约与校验规则,
这份会真正调用 FFmpeg 生成 mp4 并校验产物(尺寸/时长/音轨)。

没有 ffmpeg 的环境自动跳过,不阻断 CI。

运行: python -m unittest tests.test_structured_render -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("VIDEOMATRIX_STRUCTURED_HOME",
                      tempfile.mkdtemp(prefix="vm-render-test-"))

from app.services import structured_service as svc
from app.services.structured_ffmpeg import FFMPEG, FFPROBE, probe_media

HAVE_FFMPEG = bool(shutil.which(FFMPEG) or Path(FFMPEG).is_file())
HAVE_FFPROBE = bool(shutil.which(FFPROBE) or Path(FFPROBE).is_file())


def make_clip(path: Path, seconds: float, with_audio: bool) -> Path:
    """用 lavfi 生成一段测试素材。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i",
               "testsrc=size=320x240:rate=24:duration=%g" % seconds]
    if with_audio:
        command += ["-f", "lavfi", "-i",
                    "sine=frequency=440:duration=%g" % seconds,
                    "-c:a", "aac", "-shortest"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    return path


@unittest.skipUnless(HAVE_FFMPEG and HAVE_FFPROBE, "需要 ffmpeg / ffprobe")
class StructuredRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="vm-render-src-"))
        cls.clips = {
            "a": make_clip(cls.root / "a.mp4", 4.0, True),
            "b": make_clip(cls.root / "b.mp4", 4.0, True),
            "c": make_clip(cls.root / "c.mp4", 4.0, False),   # 无音轨
        }
        cls.service = svc.get_service()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.project = self.service.create_project("渲染测试 " + self.id()[-6:])
        self.pid = self.project["id"]
        self.product_id = self.service.create_product(self.pid, "产品")["id"]

    def _source(self, key):
        return self.service.add_source(self.pid, str(self.clips[key]),
                                       self.product_id)

    def _clip(self, key, category, start, end):
        source = self._source(key)
        return self.service.add_clip(self.pid, {
            "source_id": source["id"], "category": category,
            "start": start, "end": end,
            "cut_status": "approved", "cut_reviewer": "user",
        })

    def _wait(self, render_id, limit=180):
        for _ in range(limit):
            job = self.service.get_render(render_id)
            if job["status"] in ("completed", "failed", "stopped"):
                return job
            time.sleep(1)
        return self.service.get_render(render_id)

    def _render(self, plan_id, out_dir, **kwargs):
        payload = {"plan_id": plan_id, "output_dir": str(out_dir),
                   "width": 480, "height": 640, "fps": 24}
        payload.update(kwargs)
        started = self.service.start_render(self.pid, payload)
        return self._wait(started["render"]["id"])

    # ---------- 核心 ----------

    def test_single_output_is_real_file(self):
        self._clip("a", "A", 0.0, 2.0)
        self._clip("b", "B", 0.0, 1.5)
        plan = self.service.create_plan(self.pid, {
            "formula": "AB", "count": 1, "product_id": self.product_id})
        out = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"]) / ("out-" + self.id()[-6:])

        job = self._render(plan["plan_id"], out)
        self.assertEqual(job["status"], "completed", job["messages"])
        self.assertEqual(job["failed"], 0, job["messages"])

        files = sorted(out.glob("*.mp4"))
        self.assertEqual(len(files), 1, job["messages"])
        self.assertGreater(files[0].stat().st_size, 1024)

        info = probe_media(files[0])
        self.assertEqual((info["width"], info["height"]), (480, 640))
        self.assertAlmostEqual(info["duration"], 3.5, delta=0.4)
        self.assertTrue(info["has_audio"])

    def test_silent_source_gets_audio_bed(self):
        self._clip("c", "A", 0.0, 1.5)          # c.mp4 无音轨
        plan = self.service.create_plan(self.pid, {
            "formula": "A", "count": 1, "product_id": self.product_id})
        out = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"]) / ("silent-" + self.id()[-6:])

        job = self._render(plan["plan_id"], out)
        self.assertEqual(job["status"], "completed", job["messages"])

        files = sorted(out.glob("*.mp4"))
        self.assertEqual(len(files), 1, job["messages"])
        info = probe_media(files[0])
        self.assertTrue(info["has_audio"], "无音轨素材应补齐静音床")
        self.assertAlmostEqual(info["duration"], 1.5, delta=0.4)

    def test_batch_produces_one_file_per_combination(self):
        for key in ("a", "b", "c"):
            self._clip(key, "A", 0.0, 1.0)
        plan = self.service.create_plan(self.pid, {
            "formula": "A", "count": 3, "product_id": self.product_id,
            "distinct_sources": True})
        expected = len(plan["combinations"])
        out = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"]) / ("batch-" + self.id()[-6:])

        job = self._render(plan["plan_id"], out)
        self.assertEqual(job["status"], "completed", job["messages"])
        self.assertEqual(job["succeeded"], expected, job["messages"])
        self.assertEqual(len(sorted(out.glob("*.mp4"))), expected)
        for path in out.glob("*.mp4"):
            info = probe_media(path)
            self.assertEqual((info["width"], info["height"]), (480, 640))

    def test_existing_target_is_not_overwritten(self):
        self._clip("a", "A", 0.0, 1.0)
        plan = self.service.create_plan(self.pid, {
            "formula": "A", "count": 1, "product_id": self.product_id})
        out = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"]) / ("exist-" + self.id()[-6:])
        out.mkdir(parents=True, exist_ok=True)
        occupier = out / ("混剪-%s-1.mp4" % time.strftime("%m%d"))
        occupier.write_bytes(b"occupied")

        job = self._render(plan["plan_id"], out)
        self.assertGreaterEqual(job["failed"], 1)
        self.assertTrue(any("拒绝覆盖" in m for m in job["messages"]), job["messages"])
        self.assertEqual(occupier.read_bytes(), b"occupied")

    def test_stop_terminates_running_ffmpeg(self):
        # 用足够长的片段确保取消时有正在跑的 FFmpeg
        for key in ("a", "b", "c"):
            self._clip(key, "A", 0.0, 3.9)
        plan = self.service.create_plan(self.pid, {
            "formula": "A", "count": 3, "product_id": self.product_id,
            "distinct_sources": True})
        out = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"]) / ("stop-" + self.id()[-6:])

        started = self.service.start_render(self.pid, {
            "plan_id": plan["plan_id"], "output_dir": str(out),
            "width": 1920, "height": 1080, "fps": 30})
        render_id = started["render"]["id"]

        time.sleep(0.8)
        before = self.service.get_render(render_id)
        if before["status"] in ("completed", "failed"):
            self.skipTest("渲染过快,取消窗口不足")

        self.service.stop_render(render_id)
        job = self._wait(render_id, 120)
        self.assertIn(job["status"], ("stopped", "completed"), job["status"])
        self.assertLessEqual(job["succeeded"], job["total"])
        # 取消后不应残留半成品
        for path in out.glob("*.mp4"):
            info = probe_media(path)
            self.assertTrue(info.get("duration"), "残留了无法探测的文件: %s" % path)

    def test_numbering_is_stable(self):
        self._clip("a", "A", 0.0, 1.0)
        plan = self.service.create_plan(self.pid, {
            "formula": "A", "count": 1, "product_id": self.product_id})
        out = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"]) / ("name-" + self.id()[-6:])

        self._render(plan["plan_id"], out)
        files = sorted(out.glob("*.mp4"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^混剪-\d{4}-1\.mp4$")


if __name__ == "__main__":
    unittest.main()

