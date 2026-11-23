"""还原的 structured 模块回归测试。

覆盖 2.8.4.1 定制版的结构混剪契约:项目/产品/素材/片段/组合清单/导出。
全部断言依据逆向出的原始字节码规则(见 app/services/structured_service.py 顶部注释)。

运行: python -m pytest backend/tests/test_structured_restored.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("VIDEOMATRIX_STRUCTURED_HOME",
                      tempfile.mkdtemp(prefix="vm-structured-test-"))

from fastapi.testclient import TestClient

from app.main import app


class StructuredContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        self.home = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"])
        response = self.client.post("/api/structured/projects",
                                    json={"name": "测试项目 " + self.id()[-6:]})
        self.assertEqual(response.status_code, 200, response.text)
        self.pid = response.json()["project"]["id"]

        product = self.client.post(
            "/api/structured/projects/%s/products" % self.pid,
            json={"name": "产品 " + self.id()[-6:]})
        self.product_id = product.json()["product"]["id"]

        video = self.home / "src" / (self.id()[-6:] + ".mp4")
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)
        source = self.client.post(
            "/api/structured/projects/%s/sources" % self.pid,
            json={"path": str(video), "product_id": self.product_id})
        self.source_id = source.json()["source"]["id"]

    def _add_clip(self, category, start, end, approved=True):
        payload = {"source_id": self.source_id, "category": category,
                   "start": start, "end": end}
        if approved:
            payload["cut_status"] = "approved"
            payload["cut_reviewer"] = "user"
        return self.client.post("/api/structured/projects/%s/clips" % self.pid,
                                json=payload)

    # ---------- 项目 ----------

    def test_project_id_is_uuid_hex(self):
        self.assertEqual(len(self.pid), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in self.pid))

    def test_project_name_required(self):
        response = self.client.post("/api/structured/projects", json={"name": ""})
        self.assertEqual(response.status_code, 422)

    def test_unknown_project_returns_404(self):
        response = self.client.get("/api/structured/projects/" + "a" * 32)
        self.assertEqual(response.status_code, 404)

    def test_extra_fields_rejected(self):
        response = self.client.post("/api/structured/projects",
                                    json={"name": "x", "bogus": 1})
        self.assertEqual(response.status_code, 422)

    # ---------- 类别名 ----------

    def test_default_category_labels(self):
        response = self.client.get(
            "/api/structured/projects/%s/category-labels" % self.pid)
        labels = response.json()["labels"]
        self.assertEqual(set(labels), set("ABCDEFGT"))
        self.assertEqual(labels["A"], "爆款开头")

    def test_category_labels_reject_unknown_key(self):
        response = self.client.patch(
            "/api/structured/projects/%s/category-labels" % self.pid,
            json={"Z": "非法"})
        self.assertEqual(response.status_code, 422)

    # ---------- 片段规则 ----------

    def test_clip_id_format(self):
        first = self._add_clip("A", 0, 3.2).json()["clip"]["id"]
        second = self._add_clip("A", 0, 3.0).json()["clip"]["id"]
        self.assertEqual(first, "A1")
        self.assertEqual(second, "A2")

    def test_clip_category_validated(self):
        response = self._add_clip("Q", 0, 3)
        self.assertEqual(response.status_code, 422)

    def test_clip_minimum_duration(self):
        response = self._add_clip("B", 3.0, 3.02)
        self.assertEqual(response.status_code, 422)
        self.assertIn("0.04", response.text)

    def test_clip_end_must_exceed_start(self):
        response = self._add_clip("B", 3.0, 3.0)
        self.assertEqual(response.status_code, 422)

    def test_clip_cut_status_validated(self):
        self._add_clip("B", 3.0, 6.0)
        response = self.client.patch(
            "/api/structured/projects/%s/clips/B1" % self.pid,
            json={"cut_status": "bogus"})
        self.assertEqual(response.status_code, 422)

    def test_delete_missing_clip_returns_404(self):
        response = self.client.delete(
            "/api/structured/projects/%s/clips/A99" % self.pid)
        self.assertEqual(response.status_code, 404)

    # ---------- 组合清单 ----------

    def test_plan_blocked_without_approval(self):
        self._add_clip("A", 0, 3.0, approved=False)
        self._add_clip("B", 3.0, 6.0, approved=False)
        response = self.client.post(
            "/api/structured/projects/%s/plans" % self.pid,
            json={"formula": "AB", "count": 2, "product_id": self.product_id})
        self.assertEqual(response.status_code, 422)
        self.assertIn("已批准切点", response.text)

    def test_plan_requires_leading_a(self):
        self._add_clip("A", 0, 3.0)
        self._add_clip("B", 3.0, 6.0)
        response = self.client.post(
            "/api/structured/projects/%s/plans" % self.pid,
            json={"formula": "BA", "count": 2, "product_id": self.product_id})
        self.assertEqual(response.status_code, 422)

    def test_plan_count_upper_bound(self):
        self._add_clip("A", 0, 3.0)
        self._add_clip("B", 3.0, 6.0)
        response = self.client.post(
            "/api/structured/projects/%s/plans" % self.pid,
            json={"formula": "AB", "count": 600, "product_id": self.product_id})
        self.assertEqual(response.status_code, 422)

    def test_plan_generates_combinations(self):
        self._add_clip("A", 0, 3.0)
        self._add_clip("B", 3.0, 6.0)
        response = self.client.post(
            "/api/structured/projects/%s/plans" % self.pid,
            json={"formula": "AB", "count": 3, "product_id": self.product_id})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("plan_id", body)
        self.assertTrue(0 < len(body["combinations"]) <= 3)
        for combo in body["combinations"]:
            self.assertEqual(len(combo["clip_ids"]), 2)

    # ---------- 导出 ----------

    def test_render_requires_absolute_output(self):
        self._add_clip("A", 0, 3.0)
        plan = self.client.post(
            "/api/structured/projects/%s/plans" % self.pid,
            json={"formula": "A", "count": 1, "product_id": self.product_id}).json()
        response = self.client.post(
            "/api/structured/projects/%s/renders" % self.pid,
            json={"plan_id": plan["plan_id"], "output_dir": "relative/out"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("绝对路径", response.text)

    def test_render_dimensions_must_be_even(self):
        self._add_clip("A", 0, 3.0)
        plan = self.client.post(
            "/api/structured/projects/%s/plans" % self.pid,
            json={"formula": "A", "count": 1, "product_id": self.product_id}).json()
        response = self.client.post(
            "/api/structured/projects/%s/renders" % self.pid,
            json={"plan_id": plan["plan_id"], "output_dir": str(self.home / "out"),
                  "width": 1081, "height": 1920})
        self.assertEqual(response.status_code, 422)

    def test_render_lifecycle(self):
        self._add_clip("A", 0, 3.0)
        plan = self.client.post(
            "/api/structured/projects/%s/plans" % self.pid,
            json={"formula": "A", "count": 1, "product_id": self.product_id}).json()
        out = self.home / "out"
        started = self.client.post(
            "/api/structured/projects/%s/renders" % self.pid,
            json={"plan_id": plan["plan_id"], "output_dir": str(out)})
        self.assertEqual(started.status_code, 200, started.text)
        render_id = started.json()["render"]["id"]

        status = self.client.get("/api/structured/renders/" + render_id)
        self.assertEqual(status.status_code, 200)
        self.assertIn("status", status.json()["render"])

        stopped = self.client.post("/api/structured/renders/%s/stop" % render_id)
        self.assertEqual(stopped.status_code, 200)

    def test_stop_all_renders(self):
        response = self.client.post("/api/structured/renders/stop-all")
        self.assertEqual(response.status_code, 200)
        self.assertIn("stopped", response.json())


class StructuredStorageTests(unittest.TestCase):
    def test_persistence_is_json(self):
        client = TestClient(app)
        home = Path(os.environ["VIDEOMATRIX_STRUCTURED_HOME"])
        response = client.post("/api/structured/projects",
                               json={"name": "持久化检查"})
        pid = response.json()["project"]["id"]
        stored = home / "projects" / (pid + ".json")
        self.assertTrue(stored.is_file(), stored)
        self.assertIn("持久化检查", stored.read_text(encoding="utf-8"))
        self.assertFalse(stored.with_suffix(".db").exists())


if __name__ == "__main__":
    unittest.main()

