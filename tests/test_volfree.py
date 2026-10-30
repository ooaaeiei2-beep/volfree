#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""volfree 单元测试（unittest，零依赖）

运行：
    cd volfree && python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import volfree as vf


class TestMountParsing(unittest.TestCase):
    """mount 输出解析：必须正确处理含空格与中文的挂载点，并过滤系统卷。"""

    SAMPLE = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse, protect)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
/dev/disk3s3 on /Volumes/Recovery (apfs, local, journaled, nobrowse)
/dev/disk5s1 on /Volumes/FUJIFILM PS Plug-in Installer (hfs, local, read-only, nobrowse)
/dev/disk6s1 on /Volumes/My Passport (exfat, local, nodev, nosuid, noowners, fskit)
/dev/disk7s1 on /Volumes/我的 移动盘 (exfat, local, noowners)
"""

    def test_keeps_user_volumes_with_spaces(self):
        got = vf.parse_mount_output(self.SAMPLE)
        points = [m for _, m, _ in got]
        self.assertIn("/Volumes/My Passport", points)
        self.assertIn("/Volumes/FUJIFILM PS Plug-in Installer", points)
        self.assertIn("/Volumes/我的 移动盘", points)

    def test_filters_system_mounts(self):
        points = [m for _, m, _ in vf.parse_mount_output(self.SAMPLE)]
        self.assertNotIn("/", points)
        self.assertFalse(any(p.startswith("/System/Volumes") for p in points))
        self.assertNotIn("/dev", points)

    def test_device_and_options_parsed(self):
        got = vf.parse_mount_output(self.SAMPLE)
        entry = [x for x in got if x[1] == "/Volumes/My Passport"][0]
        self.assertEqual(entry[0], "/dev/disk6s1")
        self.assertIn("exfat", entry[2])


class TestLsofParsing(unittest.TestCase):
    """lsof -F pcnfa0t 字段解析：NUL 终止 name，支持中文与空格路径。"""

    RAW = (
        b"p3424\n"
        b"cOFD_Thumbnail\n"
        b"f3\n"
        b"ar\n"
        b"tREG\n"
        b"n/Volumes/My Passport/.Trashes/501/macOS\xe4\xb8\x8b\xe8\xbd\xbd/\xe9\x80\x9a\xe7\x9f\xa5.ofd\x00"
        b"f4\n"
        b"ar\n"
        b"tREG\n"
        b"n/Volumes/My Passport/.Trashes/501/macOS\xe4\xb8\x8b\xe8\xbd\xbd/\xe9\x80\x9a\xe7\x9f\xa5 (1).ofd\x00"
        b"p999\n"
        b"czsh\n"
        b"fcwd\n"
        b"a \n"
        b"tDIR\n"
        b"n/Volumes/My Passport/work\x00"
        b"f5\n"
        b"aw\n"
        b"tREG\n"
        b"n/Volumes/My Passport/a.txt\x00"
    )

    def test_process_count_and_fds(self):
        blockers = vf.parse_lsof_field_output(self.RAW)
        self.assertEqual(len(blockers), 2)
        self.assertEqual(blockers[0].pid, 3424)
        self.assertEqual(blockers[0].command, "OFD_Thumbnail")
        self.assertEqual(len(blockers[0].handles), 2)
        self.assertEqual(blockers[1].pid, 999)
        self.assertEqual(len(blockers[1].handles), 2)

    def test_nul_terminated_names_keep_spaces_and_unicode(self):
        blockers = vf.parse_lsof_field_output(self.RAW)
        names = [h.name for h in blockers[0].handles]
        self.assertTrue(all(n.startswith("/Volumes/My Passport/.Trashes/501/") for n in names))
        self.assertIn("通知", names[0])
        self.assertTrue(names[1].endswith(" (1).ofd"))

    def test_access_and_type_fields(self):
        blockers = vf.parse_lsof_field_output(self.RAW)
        self.assertEqual(blockers[0].handles[0].access, "r")
        self.assertEqual(blockers[0].handles[0].ftype, "REG")
        self.assertEqual(blockers[1].handles[0].fd, "cwd")
        self.assertEqual(blockers[1].handles[1].access, "w")

    def test_empty_input(self):
        self.assertEqual(vf.parse_lsof_field_output(b""), [])

    def test_link_count_field(self):
        """字段 k 是内核给的 link count，0 表示已 unlink。"""
        raw = b"p100\ncfind\nf3\nar\nk0\nn/Volumes/X/gone.txt\0"
        b = vf.parse_lsof_field_output(raw)
        self.assertEqual(b[0].handles[0].link_count, 0)

    def test_missing_link_count_is_none(self):
        raw = b"p100\ncfind\nf3\nar\nn/Volumes/X/a.txt\0"
        b = vf.parse_lsof_field_output(raw)
        self.assertIsNone(b[0].handles[0].link_count)


class TestLinkCountProbe(unittest.TestCase):
    """exFAT(fskit) 对已删除文件仍报 link count=1，
    必须识别出该卷不可靠，否则 lsof 的 +L1 会给出假绿。"""

    def _scan(self, raw, missing):
        vol = vf.Volume(device="/dev/disk9s1", mountpoint="/Volumes/X", fstype="exfat")
        orig_cap, orig_exists = vf.capture_bounded, vf.path_exists
        vf.capture_bounded = lambda cmd, limit, timeout: (0, raw, "", False, False)
        vf.path_exists = lambda p: False if p in missing else True
        try:
            blockers, warnings = vf.scan_blockers(vol)
        finally:
            vf.capture_bounded, vf.path_exists = orig_cap, orig_exists
        return vol, blockers, warnings

    def test_exfat_missing_but_link_count_one_is_unreliable(self):
        raw = b"p1\ncA\nf3\nar\nk1\nn/Volumes/X/gone.txt\0"
        vol, blockers, warnings = self._scan(raw, missing={"/Volumes/X/gone.txt"})
        self.assertIs(vol.link_count_reliable, False)
        self.assertTrue(any("link count" in w for w in warnings))

    def test_link_count_zero_marks_reliable(self):
        raw = b"p1\ncA\nf3\nar\nk0\nn/Volumes/X/gone.txt\0"
        vol, blockers, warnings = self._scan(raw, missing=set())
        self.assertIs(vol.link_count_reliable, True)
        self.assertTrue(blockers[0].handles[0].is_deleted)

    def test_all_present_leaves_reliability_unknown(self):
        raw = b"p1\ncA\nf3\nar\nk1\nn/Volumes/X/a.txt\0"
        vol, _, _ = self._scan(raw, missing=set())
        self.assertIsNone(vol.link_count_reliable)


class TestPathExistence(unittest.TestCase):
    """核心判据：文件已删除但句柄仍被持有。"""

    def test_existing_path(self):
        self.assertIs(vf.path_exists(__file__), True)

    def test_missing_path_is_false(self):
        self.assertIs(vf.path_exists("/Volumes/__volfree_no_such__/x.ofd"), False)

    def test_parent_missing_is_false(self):
        self.assertIs(vf.path_exists("/nonexistent-dir-xyz/a/b/c"), False)


class TestRiskAssessment(unittest.TestCase):
    """定级规则必须与「数据安全」严格对应。"""

    def _blocker(self, exe="", handles=None, pid=4000):
        b = vf.Blocker(pid=pid, command="test", exe=exe)
        b.handles = handles or []
        return b

    def _handle(self, access="r", exists=True, fd="3", name="/Volumes/X/a.txt"):
        return vf.Handle(fd=fd, access=access, ftype="REG", name=name, exists=exists)

    def test_deleted_readonly_is_safe(self):
        b = self._blocker("/System/.../OFD_Thumbnail",
                          [self._handle(exists=False), self._handle(exists=False, fd="4")])
        vf.assess(b)
        self.assertEqual(b.level, "SAFE")
        self.assertIn("已删除且为只读", b.reason)

    def test_write_handle_is_risky(self):
        b = self._blocker("/Applications/Foo.app/Contents/MacOS/Foo",
                          [self._handle(access="w", exists=True)])
        vf.assess(b)
        self.assertEqual(b.level, "RISKY")

    def test_readwrite_handle_is_risky(self):
        b = self._blocker("/Applications/Foo.app/Contents/MacOS/Foo",
                          [self._handle(access="u", exists=True)])
        vf.assess(b)
        self.assertEqual(b.level, "RISKY")

    def test_appex_readonly_is_safe(self):
        b = self._blocker("/System/Library/QuickLook/Thumbnail.appex/Contents/MacOS/X",
                          [self._handle()])
        vf.assess(b)
        self.assertEqual(b.level, "SAFE")

    def test_system_helper_readonly_is_caution(self):
        b = self._blocker("/usr/libexec/somehelper", [self._handle()])
        vf.assess(b)
        self.assertEqual(b.level, "CAUTION")

    def test_user_app_readonly_is_caution(self):
        b = self._blocker("/Applications/Preview.app/Contents/MacOS/Preview", [self._handle()])
        vf.assess(b)
        self.assertEqual(b.level, "CAUTION")

    def test_cwd_only_is_caution_with_specific_reason(self):
        """终端工作目录在卷上：第二常见的阻塞原因，理由需可操作。"""
        b = self._blocker("/bin/zsh", [
            vf.Handle(fd="cwd", access="", ftype="DIR", name="/Volumes/My Passport/work", exists=True)
        ])
        vf.assess(b)
        self.assertEqual(b.level, "CAUTION")
        self.assertIn("工作目录", b.reason)

    def test_pid1_is_never(self):
        b = self._blocker("/sbin/launchd", [self._handle()], pid=1)
        vf.assess(b)
        self.assertEqual(b.level, "NEVER")

    def test_protected_executable_is_never(self):
        b = self._blocker("/System/Library/CoreServices/WindowServer", [self._handle()], pid=800)
        vf.assess(b)
        self.assertEqual(b.level, "NEVER")

    def test_self_pid_is_never(self):
        b = self._blocker("/usr/bin/python3", [self._handle()], pid=os.getpid())
        vf.assess(b)
        self.assertEqual(b.level, "NEVER")

    def test_unlinked_write_is_caution_not_safe_not_risky(self):
        """已删除 + 可写：不能算安全（有进行中的写入会被放弃），
        也不该算危险（没有现存文件可损坏）。见 docs/文件系统语义核查.md 第三节。"""
        b = self._blocker("/Applications/Foo.app/Contents/MacOS/Foo",
                          [self._handle(access="w", exists=False)])
        vf.assess(b)
        self.assertEqual(b.level, "CAUTION")
        self.assertIn("已被删除", b.reason)
        self.assertIn("不会损坏其他文件", b.reason)

    def test_link_count_zero_counts_as_deleted(self):
        """link count 为 0 是内核确认的已 unlink，与 stat 结果独立。"""
        h = self._handle(exists=None)
        h.link_count = 0
        self.assertTrue(h.is_deleted)

    def test_link_count_one_but_missing_is_deleted(self):
        """exFAT(fskit) 对已删除文件仍报 link count=1，此时只能靠路径校验。"""
        h = self._handle(exists=False)
        h.link_count = 1
        self.assertTrue(h.is_deleted)

    def test_link_count_one_and_exists_is_not_deleted(self):
        h = self._handle(exists=True)
        h.link_count = 1
        self.assertFalse(h.is_deleted)


class TestHandleHelpers(unittest.TestCase):
    def test_pseudo_handle_detection(self):
        # cwd 的 name 是真实绝对路径，属于有效占用，不算伪句柄
        self.assertFalse(vf.Handle(fd="cwd", name="/Volumes/X").is_pseudo)
        self.assertTrue(vf.Handle(fd="3", name="localhost:8080").is_pseudo)
        self.assertTrue(vf.Handle(fd="3", name="").is_pseudo)

    def test_writing_detection(self):
        self.assertTrue(vf.Handle(fd="3", access="w", name="/a").is_writing)
        self.assertTrue(vf.Handle(fd="3", access="u", name="/a").is_writing)
        self.assertFalse(vf.Handle(fd="3", access="r", name="/a").is_writing)

    def test_all_deleted_ignores_pseudo(self):
        b = vf.Blocker(pid=1, command="x")
        b.handles = [vf.Handle(fd="3", name="localhost:8080"),
                     vf.Handle(fd="4", name="/Volumes/X/a", exists=False)]
        self.assertTrue(b.all_deleted)

    def test_all_deleted_false_when_one_exists(self):
        b = vf.Blocker(pid=1, command="x")
        b.handles = [vf.Handle(fd="3", name="/a", exists=False),
                     vf.Handle(fd="4", name="/b", exists=True)]
        self.assertFalse(b.all_deleted)


class TestMappingWritability(unittest.TestCase):
    """lsof 的 txt 句柄 access 恒为空，必须另外探保护位。

    实测：mmap(2) 后 close(fd) 的进程仍在写盘，若按「不含 w/u 就是只读」处理，
    会被判成可安全推出。见 docs/判定情况全表.md 第 6 节。
    """

    def _mapping(self, mapped, exists=True, name="/Volumes/X/map.bin"):
        return vf.Handle(fd="txt", access="", ftype="REG", name=name,
                         exists=exists, link_count=1 if exists else 0,
                         mapped_writable=mapped)

    def _blocker(self, handles):
        b = vf.Blocker(pid=4000, command="t",
                       exe="/Applications/Foo.app/Contents/MacOS/Foo")
        b.handles = handles
        return b

    def test_txt_is_recognized_as_mapping(self):
        self.assertTrue(self._mapping(True).is_mapping)
        self.assertFalse(vf.Handle(fd="3", access="r", ftype="REG", name="/a").is_mapping)
        self.assertFalse(vf.Handle(fd="cwd", access="", ftype="DIR", name="/a").is_mapping)

    def test_writable_mapping_is_writing(self):
        h = self._mapping(True)
        self.assertEqual(h.write_state, "yes")
        self.assertTrue(h.is_writing)

    def test_readonly_mapping_is_not_writing(self):
        h = self._mapping(False)
        self.assertEqual(h.write_state, "no")
        self.assertFalse(h.is_writing)

    def test_unprobed_mapping_is_unknown_not_readonly(self):
        """取不到保护位时绝不能当成只读——这是最容易犯的假安全。"""
        h = self._mapping(None)
        self.assertEqual(h.write_state, "unknown")
        self.assertTrue(h.is_writing)
        self.assertFalse(h.is_certainly_read_only)

    def test_writable_mapping_of_named_file_is_risky(self):
        b = self._blocker([self._mapping(True)])
        vf.assess(b)
        self.assertEqual(b.level, "RISKY")

    def test_writable_mapping_of_unlinked_file_is_caution_not_safe(self):
        b = self._blocker([self._mapping(True, exists=False)])
        vf.assess(b)
        self.assertEqual(b.level, "CAUTION")
        self.assertIn("已被删除", b.reason)

    def test_readonly_mapping_of_unlinked_file_is_safe(self):
        b = self._blocker([self._mapping(False, exists=False)])
        vf.assess(b)
        self.assertEqual(b.level, "SAFE")

    def test_unknown_mapping_reason_admits_uncertainty(self):
        """按最坏情况定级时，理由必须说明是「没读到」，不能说成「持有写入句柄」。"""
        b = self._blocker([self._mapping(None)])
        vf.assess(b)
        self.assertEqual(b.level, "RISKY")
        self.assertIn("无法确认", b.reason)


class TestMixedWriteHandles(unittest.TestCase):
    """同时写有名文件和无名临时文件时，不能因为后者而降级。"""

    def _blocker(self, handles):
        b = vf.Blocker(pid=4000, command="t",
                       exe="/Applications/Foo.app/Contents/MacOS/Foo")
        b.handles = handles
        return b

    def _named_w(self):
        return vf.Handle(fd="3", access="u", ftype="REG",
                         name="/Volumes/X/doc.psd", exists=True, link_count=1)

    def _named_r(self):
        return vf.Handle(fd="3", access="r", ftype="REG",
                         name="/Volumes/X/doc.psd", exists=True, link_count=1)

    def _unlinked_w(self):
        return vf.Handle(fd="5", access="u", ftype="REG",
                         name="/Volumes/X/tmp", exists=False, link_count=0)

    def test_named_write_plus_unlinked_write_is_risky(self):
        """有名字的可写文件才是「会损坏现存文件」的判据，不能被临时文件掩盖。"""
        b = self._blocker([self._named_w(), self._unlinked_w()])
        vf.assess(b)
        self.assertEqual(b.level, "RISKY")
        self.assertFalse(b.all_writes_unlinked)

    def test_unlinked_write_alone_stays_caution(self):
        b = self._blocker([self._unlinked_w()])
        vf.assess(b)
        self.assertEqual(b.level, "CAUTION")
        self.assertTrue(b.all_writes_unlinked)

    def test_readonly_named_plus_unlinked_write_stays_caution(self):
        """只读的有名文件不构成损坏风险，仍是 CAUTION。"""
        b = self._blocker([self._named_r(), self._unlinked_w()])
        vf.assess(b)
        self.assertEqual(b.level, "CAUTION")


class TestMappingProbe(unittest.TestCase):
    """scan_blockers 必须为映射句柄补探保护位，探测失败保持未知。"""

    def _scan(self, raw, table):
        vol = vf.Volume(device="/dev/disk9s1", mountpoint="/Volumes/X", fstype="exfat")
        orig = (vf.capture_bounded, vf.path_exists, vf.scan_writable_mappings)
        vf.capture_bounded = lambda cmd, limit, timeout: (0, raw, "", False, False)
        vf.path_exists = lambda p: True
        vf.scan_writable_mappings = lambda pid: table
        try:
            blockers, _ = vf.scan_blockers(vol)
        finally:
            (vf.capture_bounded, vf.path_exists,
             vf.scan_writable_mappings) = orig
        return blockers

    def test_probe_fills_mapped_writable(self):
        raw = b"p1\ncA\nftxt\na\ntREG\nk1\nn/Volumes/X/map.bin\0"
        blockers = self._scan(raw, {"/Volumes/X/map.bin": True})
        self.assertIs(blockers[0].handles[0].mapped_writable, True)
        self.assertTrue(blockers[0].has_write)

    def test_probe_failure_leaves_unknown(self):
        raw = b"p1\ncA\nftxt\na\ntREG\nk1\nn/Volumes/X/map.bin\0"
        blockers = self._scan(raw, {})
        self.assertIsNone(blockers[0].handles[0].mapped_writable)
        self.assertTrue(blockers[0].has_write)

    def test_readonly_probe_is_not_write(self):
        raw = b"p1\ncA\nftxt\na\ntREG\nk1\nn/Volumes/X/map.bin\0"
        blockers = self._scan(raw, {"/Volumes/X/map.bin": False})
        self.assertFalse(blockers[0].has_write)


class TestWidthPadding(unittest.TestCase):
    """中英文混排表格对齐：CJK 占两列。"""

    def test_disp_width(self):
        self.assertEqual(vf.disp_width("abc"), 3)
        self.assertEqual(vf.disp_width("安全"), 4)
        self.assertEqual(vf.disp_width("安全ab"), 6)

    def test_pad_right(self):
        self.assertEqual(vf.pad("安全", 8), "安全    ")
        self.assertEqual(vf.pad("abc", 6), "abc   ")

    def test_pad_left(self):
        self.assertEqual(vf.pad("42", 6, right=True), "    42")


class TestHumanSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(vf.human_size(0), "—")
        self.assertEqual(vf.human_size(512), "512 B")
        self.assertEqual(vf.human_size(2048), "2.0 KB")
        self.assertEqual(vf.human_size(2 * 1024 ** 3), "2.0 GB")


class TestJsonOutput(unittest.TestCase):
    def test_structure(self):
        import json
        vol = vf.Volume(device="/dev/disk9s1", mountpoint="/Volumes/T",
                        fstype="exfat", external=True, ejectable=True,
                        volume_name="T", total_bytes=100, free_bytes=40)
        b = vf.Blocker(pid=42, command="Foo", exe="/Applications/Foo", level="SAFE", reason="r")
        b.handles = [vf.Handle(fd="3", access="r", ftype="REG", name="/Volumes/T/a", exists=False)]
        data = json.loads(vf.blockers_to_json(vol, [b], ["w"]))
        self.assertEqual(data["volume"]["name"], "T")
        self.assertEqual(data["blockers"][0]["handles"][0]["exists"], False)
        self.assertEqual(data["warnings"], ["w"])


class TestInterfacePayload(unittest.TestCase):
    """界面吃的是 JSON 里的机器字段，不能让它去解析中文理由。"""

    def _vol(self):
        return vf.Volume(device="/dev/disk9s1", mountpoint="/Volumes/T",
                         fstype="exfat", external=True, ejectable=True,
                         volume_name="T", total_bytes=100, free_bytes=40)

    def _named_write(self):
        b = vf.Blocker(pid=7, command="Foo", level="RISKY", reason="r")
        b.handles = [vf.Handle(fd="3", access="w", ftype="REG",
                               name="/Volumes/T/a", exists=True, link_count=1)]
        return b

    def _unlinked_write(self):
        b = vf.Blocker(pid=8, command="Bar", level="CAUTION", reason="r")
        b.handles = [vf.Handle(fd="3", access="w", ftype="REG",
                               name="/Volumes/T/tmp", exists=False, link_count=1)]
        return b

    def test_named_write_flags(self):
        f = vf.blocker_payload(self._named_write())["flags"]
        self.assertTrue(f["has_write"])
        self.assertFalse(f["all_writes_unlinked"],
                         "有名字的可写文件不能被当成「只放弃无名写入」")

    def test_unlinked_write_flags(self):
        f = vf.blocker_payload(self._unlinked_write())["flags"]
        self.assertTrue(f["has_write"])
        self.assertTrue(f["all_writes_unlinked"])

    def test_handle_exposes_write_state(self):
        h = vf.blocker_payload(self._named_write())["handles"][0]
        self.assertEqual(h["write_state"], "yes")

    def test_summary_counts(self):
        named = self._named_write()
        unlinked = self._unlinked_write()
        read_only = vf.Blocker(pid=9, command="Baz", level="CAUTION", reason="r")
        read_only.handles = [vf.Handle(fd="4", access="r", ftype="REG",
                                       name="/Volumes/T/b", exists=True)]
        p = vf.volume_payload(self._vol(), [named, unlinked, read_only], [])
        s = p["summary"]
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["risky"], 1)
        self.assertEqual(s["caution"], 2)
        self.assertEqual(s["safe"], 0)
        self.assertEqual(s["writing"], 2)
        self.assertEqual(s["abandoned_writes"], 1,
                         "只有「全部可写句柄都无名」的那个进程才算放弃写入")

    def test_summary_empty_volume(self):
        s = vf.volume_payload(self._vol(), [], [])["summary"]
        self.assertEqual((s["total"], s["writing"], s["abandoned_writes"]), (0, 0, 0))

    def test_volume_exposes_removable(self):
        self.assertIn("removable", vf.volume_payload(self._vol(), [], [])["volume"])


class TestActionParser(unittest.TestCase):
    """面向界面的三个子命令，默认值必须是「不动手」的那一侧。"""

    def test_status_json(self):
        args = vf.build_parser().parse_args(["status", "--json"])
        self.assertTrue(args.json)

    def test_end_defaults_are_conservative(self):
        args = vf.build_parser().parse_args(["end", "123"])
        self.assertEqual(args.pid, 123)
        self.assertFalse(args.force, "end 默认不得 SIGKILL")
        self.assertFalse(args.allow_risky, "end 默认不得结束正在写入的进程")
        self.assertIsNone(args.volume)

    def test_end_flags(self):
        args = vf.build_parser().parse_args(
            ["end", "123", "--volume", "T", "--force", "--allow-risky", "--json"])
        self.assertTrue(args.force)
        self.assertTrue(args.allow_risky)
        self.assertEqual(args.volume, "T")
        self.assertTrue(args.json)

    def test_eject_defaults_are_conservative(self):
        args = vf.build_parser().parse_args(["eject", "T"])
        self.assertFalse(args.force_unmount, "eject 默认不得强制卸载")


class TestClassifyKind(unittest.TestCase):
    def test_kinds(self):
        cases = [
            ("/System/Library/QuickLook/X.appex/Contents/MacOS/X", "x", "系统扩展"),
            ("/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder", "Finder", "访达"),
            ("/usr/libexec/foo", "foo", "系统进程"),
            ("/Applications/Preview.app/Contents/MacOS/Preview", "Preview", "用户应用"),
            ("/usr/bin/python3", "python3", "脚本进程"),
            ("/bin/zsh", "zsh", "终端"),
            ("/Applications/WorkBuddy AI.app/Contents/.../zsh", "zsh", "终端"),
            ("/opt/weird/bin/thing", "thing", "其他"),
        ]
        for exe, cmd, expected in cases:
            b = vf.Blocker(pid=1, command=cmd, exe=exe)
            self.assertEqual(vf.classify_kind(b), expected, msg=exe)


class TestCliParser(unittest.TestCase):
    """--no-color / --all 必须同时支持放在子命令前后。"""

    def test_global_before_subcommand(self):
        args = vf.build_parser().parse_args(["--no-color", "analyze", "X"])
        self.assertTrue(args.no_color)
        self.assertEqual(args.volume, "X")

    def test_global_after_subcommand(self):
        args = vf.build_parser().parse_args(["analyze", "X", "--no-color"])
        self.assertTrue(args.no_color)

    def test_release_defaults_are_safe(self):
        args = vf.build_parser().parse_args(["release", "X"])
        self.assertFalse(args.apply, "release 默认必须是 dry-run")
        self.assertFalse(args.force)
        self.assertFalse(args.force_unmount)
        self.assertFalse(args.yes)

    def test_analyze_json_flag(self):
        args = vf.build_parser().parse_args(["analyze", "X", "--json"])
        self.assertTrue(args.json)


class TestVolumeFlags(unittest.TestCase):
    """外接/映像判定需要回落到父盘信息。

    macOS 27 的 plist 已不再稳定提供 DeviceLocation，主判据改为
    Internal=false 且非虚拟介质。
    """

    def setUp(self):
        vf._parent_info_cache.clear()
        self._original = vf.read_volume_info

    def tearDown(self):
        vf.read_volume_info = self._original
        vf._parent_info_cache.clear()

    def test_external_from_parent(self):
        vf.read_volume_info = lambda dev: ({"DeviceLocation": "External"}
                                           if dev == "/dev/disk9" else {})
        external, ejectable, virtual = vf._volume_flags(
            {"ParentWholeDisk": "disk9", "Ejectable": True})
        self.assertTrue(external)
        self.assertTrue(ejectable)
        self.assertFalse(virtual)

    def test_disk_image_from_parent(self):
        vf.read_volume_info = lambda dev: ({"MediaName": "Disk Image",
                                            "VirtualOrPhysical": "Virtual"}
                                           if dev == "/dev/disk4" else {})
        _, _, virtual = vf._volume_flags({"ParentWholeDisk": "disk4"})
        self.assertTrue(virtual)

    def test_usb_volume_without_devicelocation(self):
        """真实外接盘：卷级 Internal=false，父盘 USB + Physical，无 DeviceLocation。"""
        vf.read_volume_info = lambda dev: ({"Internal": False, "BusProtocol": "USB",
                                            "VirtualOrPhysical": "Physical",
                                            "MediaName": "SanDisk 3.2 Gen1",
                                            "Ejectable": True}
                                           if dev == "/dev/disk7" else {})
        external, ejectable, virtual = vf._volume_flags(
            {"ParentWholeDisk": "disk7", "Internal": False, "Ejectable": True})
        self.assertTrue(external, "外接盘不能被误判为内置")
        self.assertTrue(ejectable)
        self.assertFalse(virtual)

    def test_internal_disk_is_not_external(self):
        vf.read_volume_info = lambda dev: {"Internal": True}
        external, _, _ = vf._volume_flags({"ParentWholeDisk": "disk0", "Internal": True})
        self.assertFalse(external)

    def test_virtual_is_not_external(self):
        vf.read_volume_info = lambda dev: {"Internal": False,
                                           "VirtualOrPhysical": "Virtual",
                                           "MediaName": "Disk Image"}
        external, _, virtual = vf._volume_flags({"ParentWholeDisk": "disk4"})
        self.assertFalse(external)
        self.assertTrue(virtual)

    def test_parent_info_is_cached(self):
        calls = []

        def counting(dev):
            calls.append(dev)
            return {"Internal": False}

        vf.read_volume_info = counting
        vf._volume_flags({"ParentWholeDisk": "disk7"})
        vf._volume_flags({"ParentWholeDisk": "disk7"})
        self.assertEqual(calls, ["/dev/disk7"], "同一父盘只应查询一次")

    def test_no_parent_no_crash(self):
        external, ejectable, virtual = vf._volume_flags({})
        self.assertEqual((external, ejectable, virtual), (False, False, False))


class TestAccessLevel(unittest.TestCase):
    def test_returns_known_level(self):
        level, desc = vf.detect_access_level()
        self.assertIn(level, ("root", "full", "limited", "unknown"))
        self.assertTrue(desc)

    def test_root_short_circuits(self):
        original = os.geteuid
        os.geteuid = lambda: 0
        try:
            level, _ = vf.detect_access_level()
        finally:
            os.geteuid = original
        self.assertEqual(level, "root")


class TestEnvClues(unittest.TestCase):
    """环境线索用于弥补看不到 root 进程的盲区，至少要能安全跑完。"""

    def test_collect_on_real_volume(self):
        vols = vf.list_volumes()
        if not vols:
            self.skipTest("当前没有可诊断的卷")
        clues = vf.collect_clues(vols[0])
        self.assertIsInstance(clues, list)
        for c in clues:
            self.assertIsInstance(c, vf.EnvClue)
            self.assertTrue(c.label)
            self.assertTrue(c.detail)

    def test_clue_warn_flag_is_bool(self):
        c = vf.EnvClue("Spotlight", "索引中", True)
        self.assertTrue(c.warn)
        self.assertFalse(vf.EnvClue("x", "y").warn)


class TestLsofInvocation(unittest.TestCase):
    """回归保护：lsof 必须带 +c 0，否则长命令名被截断为 9 字符。"""

    def test_uses_full_command_names(self):
        captured = {}

        def fake_capture(cmd, limit, timeout):
            captured["cmd"] = cmd
            return 1, b"", "", False, False

        original = vf.capture_bounded
        vf.capture_bounded = fake_capture
        try:
            vf.scan_blockers(vf.Volume(device="/dev/disk9s1",
                                       mountpoint="/Volumes/X", fstype="exfat"))
        finally:
            vf.capture_bounded = original

        cmd = captured["cmd"]
        self.assertIn("+c", cmd, "缺少 +c 会导致命令名被截断")
        idx = cmd.index("+c")
        self.assertEqual(cmd[idx + 1], "0")

    def test_requests_link_count_field(self):
        """必须请求字段 k，否则拿不到内核的 link count。"""
        captured = {}

        def fake_capture(cmd, limit, timeout):
            captured["cmd"] = cmd
            return 1, b"", "", False, False

        original = vf.capture_bounded
        vf.capture_bounded = fake_capture
        try:
            vf.scan_blockers(vf.Volume(device="/dev/disk9s1",
                                       mountpoint="/Volumes/X", fstype="exfat"))
        finally:
            vf.capture_bounded = original

        idx = captured["cmd"].index("-F")
        self.assertIn("k", captured["cmd"][idx + 1])


class TestDiagnoseIntegration(unittest.TestCase):
    """对真实卷跑完整诊断，确保各环节能串起来。"""

    def test_diagnose_returns_four_parts(self):
        vols = vf.list_volumes()
        if not vols:
            self.skipTest("当前没有可诊断的卷")
        blockers, warnings, access, clues = vf.diagnose(vols[0])
        self.assertIsInstance(blockers, list)
        self.assertIsInstance(warnings, list)
        self.assertEqual(len(access), 2)
        self.assertIn(access[0], ("root", "full", "limited", "unknown"))
        self.assertIsInstance(clues, list)


class TestWatchCommand(unittest.TestCase):
    """`watch`：面板关闭时更新菜单栏图标用的轻量通道。

    三条必须守住：
      ① 没有挂载点时**不产生任何子进程**（没盘时零开销，这是敢开空闲轮询的前提）；
      ② 绝不调用 `diskutil`（那 270ms 就是这条命令存在的理由）；
      ③ 色调规则与界面 `Models.swift` 的 `tone` 一致。
    """

    def _invoke(self, mount_table, wanted, blockers_by_mp=None):
        """跑一次 cmd_watch，返回 (返回码, 解析后的 JSON, 所有子进程调用)。"""
        blockers_by_mp = blockers_by_mp or {}
        calls = []

        def fake_run(cmd, timeout=20.0):
            calls.append(list(cmd))
            if cmd[0] == "mount":
                body = "".join(f"{dev} on {mp} (apfs, local)\n" for dev, mp in mount_table)
                return 0, body, ""
            return 0, "", ""

        orig = (vf.run, vf.scan_blockers, vf.enrich_and_assess)
        vf.run = fake_run
        vf.scan_blockers = lambda vol: (list(blockers_by_mp.get(vol.mountpoint, [])), [])
        vf.enrich_and_assess = lambda bs: bs
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = vf.cmd_watch(_Args(volume=wanted), None)
            return code, json.loads(buf.getvalue()), calls
        finally:
            vf.run, vf.scan_blockers, vf.enrich_and_assess = orig

    def test_no_mountpoints_spawns_nothing(self):
        """没给挂载点就直接返回，连 mount 都不跑——没盘时应当零开销。"""
        code, payload, calls = self._invoke([], None)
        self.assertEqual(code, 0)
        self.assertEqual(payload["volumes"], [])
        self.assertEqual(calls, [], "没有挂载点时不应产生任何子进程")

    def test_never_calls_diskutil(self):
        """轻量通道的全部意义就是不查 diskutil。"""
        _, _, calls = self._invoke([("/dev/disk9s1", "/Volumes/X")], ["/Volumes/X"])
        flat = " ".join(" ".join(c) for c in calls)
        self.assertNotIn("diskutil", flat)

    def test_skips_mountpoint_no_longer_mounted(self):
        """刚被拔掉的挂载点直接跳过，不猜。"""
        _, payload, _ = self._invoke([("/dev/disk9s1", "/Volumes/Other")], ["/Volumes/Gone"])
        self.assertEqual(payload["volumes"], [])

    def test_tone_matches_ui_rule(self):
        """与界面同一条规则：有 RISKY 是红，否则有人占着是黄，没人占是绿。"""
        table = [("/dev/disk9s1", "/Volumes/X")]
        cases = [
            ([], "safe"),
            ([vf.Blocker(pid=11, command="a", level="CAUTION")], "caution"),
            ([vf.Blocker(pid=12, command="b", level="SAFE")], "caution"),
            ([vf.Blocker(pid=13, command="c", level="RISKY")], "danger"),
            ([vf.Blocker(pid=14, command="d", level="CAUTION"),
              vf.Blocker(pid=15, command="e", level="RISKY")], "danger"),
        ]
        for blockers, expected in cases:
            with self.subTest(expected=expected, n=len(blockers)):
                _, payload, _ = self._invoke(table, ["/Volumes/X"],
                                             {"/Volumes/X": blockers})
                self.assertEqual(payload["volumes"][0]["tone"], expected)

    def test_reports_counts_used_by_ui(self):
        """total/risky/writing 要与 blockers 对得上，界面据此决定文案。"""
        table = [("/dev/disk9s1", "/Volumes/X")]
        blockers = [vf.Blocker(pid=21, command="a", level="RISKY"),
                    vf.Blocker(pid=22, command="b", level="CAUTION")]
        _, payload, _ = self._invoke(table, ["/Volumes/X"], {"/Volumes/X": blockers})
        v = payload["volumes"][0]
        self.assertEqual(v["total"], 2)
        self.assertEqual(v["risky"], 1)


class TestVolumeBinding(unittest.TestCase):
    """P0-1：发信号前必须复核「该 pid 此刻仍持有这个卷」。

    只校验 pid + 启动时间 + exe，挡不住「扫描后关掉本卷句柄、PID 又被复用」。
    """

    START = 1_700_000_000
    EXE = "/Applications/A.app/Contents/MacOS/A"

    def _blocker(self, mountpoint="/Volumes/X"):
        return vf.Blocker(pid=4242, command="A", exe=self.EXE,
                          start_time=self.START, device="/dev/disk9s1",
                          mountpoint=mountpoint)

    def _verify(self, held, mountpoint="/Volumes/X"):
        b = self._blocker(mountpoint)
        orig = (vf.proc_start_time, vf.proc_executable_path, vf.pid_holds_volume)
        vf.proc_start_time = lambda pid: self.START
        vf.proc_executable_path = lambda pid: self.EXE
        vf.pid_holds_volume = lambda pid, mp: held
        try:
            return vf.verify_identity(b)
        finally:
            (vf.proc_start_time, vf.proc_executable_path,
             vf.pid_holds_volume) = orig

    def test_pid_reused_on_other_volume_is_rejected(self):
        """pid 与启动时间都没变，但句柄已不在本卷 —— 必须拒绝。"""
        ok, why = self._verify(False)
        self.assertFalse(ok)
        self.assertIn("不再持有本卷", why)

    def test_still_holding_same_volume_passes(self):
        ok, _ = self._verify(True)
        self.assertTrue(ok)

    def test_unknown_binding_is_rejected(self):
        """复核不了就不动手：宁可跳过，也不赌。"""
        ok, why = self._verify(None)
        self.assertFalse(ok)
        self.assertIn("无法复核", why)

    def test_blocker_without_mountpoint_keeps_old_behaviour(self):
        """没有卷信息（老调用方/单测构造）时退回原有三项校验。"""
        ok, _ = self._verify(False, mountpoint="")
        self.assertTrue(ok)

    def test_scan_stamps_volume_identity(self):
        raw = b"p1\ncA\nf3\nar\nk1\nn/Volumes/X/a.txt\0"
        vol = vf.Volume(device="/dev/disk9s1", mountpoint="/Volumes/X", fstype="exfat")
        orig = (vf.capture_bounded, vf.path_exists)
        vf.capture_bounded = lambda cmd, limit, timeout: (0, raw, "", False, False)
        vf.path_exists = lambda p: True
        try:
            blockers, _ = vf.scan_blockers(vol)
        finally:
            vf.capture_bounded, vf.path_exists = orig
        self.assertEqual(blockers[0].device, "/dev/disk9s1")
        self.assertEqual(blockers[0].mountpoint, "/Volumes/X")

    def test_pid_holds_volume_matches_subtree_only(self):
        """句柄路径必须落在该挂载点之内，同名前缀不算。"""
        class FakeProc:
            returncode = 0
            stdout = "p4242\nn/Volumes/X2/other.txt\nn/Volumes/X/a.txt\n"

        orig = vf.subprocess.run
        vf.subprocess.run = lambda cmd, **kw: FakeProc()
        try:
            self.assertTrue(vf.pid_holds_volume(4242, "/Volumes/X"))
            self.assertFalse(vf.pid_holds_volume(4242, "/Volumes/Y"))
        finally:
            vf.subprocess.run = orig


class TestBoundedCapture(unittest.TestCase):
    """P1-1：lsof 输出必须有上限；超限要截断并降级结论。"""

    def test_small_output_is_complete(self):
        code, raw, err, truncated, timed_out = vf.capture_bounded(
            ["/bin/sh", "-c", "printf abc"], 1024, 10)
        self.assertEqual(raw, b"abc")
        self.assertFalse(truncated)
        self.assertFalse(timed_out)

    def test_oversize_output_is_truncated(self):
        _, raw, _, truncated, timed_out = vf.capture_bounded(
            ["/bin/sh", "-c", "yes volfree | head -c 200000"], 4096, 10)
        self.assertTrue(truncated)
        self.assertFalse(timed_out)
        self.assertLessEqual(len(raw), 4096)

    def _scan_with(self, result):
        vol = vf.Volume(device="/dev/disk9s1", mountpoint="/Volumes/X", fstype="exfat")
        orig = vf.capture_bounded
        vf.capture_bounded = lambda cmd, limit, timeout: result
        try:
            blockers, warnings = vf.scan_blockers(vol)
        finally:
            vf.capture_bounded = orig
        return vol, blockers, warnings

    def test_truncated_scan_degrades_conclusion(self):
        vol, blockers, warnings = self._scan_with((0, b"", "", True, False))
        self.assertIs(vol.scan_complete, False)
        self.assertTrue(any("截断" in w for w in warnings))
        payload = json.loads(vf.blockers_to_json(vol, blockers, warnings))
        self.assertIs(payload["summary"]["complete"], False)

    def test_timeout_marks_incomplete(self):
        vol, _, warnings = self._scan_with((124, b"", "", False, True))
        self.assertIs(vol.scan_complete, False)
        self.assertTrue(any("超时" in w for w in warnings))

    def test_abnormal_exit_marks_incomplete(self):
        vol, _, _ = self._scan_with((2, b"", "boom", False, False))
        self.assertIs(vol.scan_complete, False)

    def test_complete_scan_stays_complete(self):
        vol, _, _ = self._scan_with((1, b"", "", False, False))
        self.assertIs(vol.scan_complete, True)

    def test_report_never_claims_safe_when_incomplete(self):
        """扫描不完整时，空列表不能渲染成「可直接推出」。"""
        vol = vf.Volume(device="/dev/disk9s1", mountpoint="/Volumes/X",
                        fstype="exfat", external=True, scan_complete=False)
        text = vf.render_report(vol, [], ["lsof 输出超限，已截断"],
                                vf.Palette(False), ("full", "完全磁盘访问"))
        self.assertIn("扫描不完整", text)
        self.assertNotIn("可直接在访达推出", text)
        self.assertNotIn("未发现占用", text)


class TestOtherUserPolicy(unittest.TestCase):
    """P1-3：其他用户的进程在 assess 阶段就标注，不必等到 EPERM 才发现。"""

    OTHER = os.getuid() + 1

    def _assess(self, uid, handles):
        b = vf.Blocker(pid=9000, command="X", uid=uid,
                       exe="/Applications/A.app/Contents/MacOS/A", handles=handles)
        vf.assess(b, self_pid=1)
        return b

    @staticmethod
    def _ro():
        return vf.Handle(fd="3", access="r", ftype="REG",
                         name="/Volumes/D/doc.psd", exists=True, link_count=1)

    @staticmethod
    def _rw():
        return vf.Handle(fd="3", access="u", ftype="REG",
                         name="/Volumes/D/doc.psd", exists=True, link_count=1)

    def test_other_user_readonly_becomes_never(self):
        b = self._assess(self.OTHER, [self._ro()])
        self.assertEqual(b.level, "NEVER")
        self.assertTrue(b.other_user)
        self.assertIn("无权终止", b.reason)

    def test_other_user_write_keeps_risky(self):
        """写入风险不能被「反正杀不掉」冲淡成低等级。"""
        b = self._assess(self.OTHER, [self._rw()])
        self.assertEqual(b.level, "RISKY")
        self.assertTrue(b.other_user)
        self.assertIn("无权终止", b.reason)

    def test_same_user_is_untouched(self):
        b = self._assess(os.getuid(), [self._ro()])
        self.assertEqual(b.level, "CAUTION")
        self.assertFalse(b.other_user)

    def test_unknown_uid_is_not_guessed(self):
        """uid 取不到时不得擅自判成「别人的进程」。"""
        b = self._assess(-1, [self._ro()])
        self.assertEqual(b.level, "CAUTION")
        self.assertFalse(b.other_user)


class TestAssessRules(unittest.TestCase):
    """P1-2：定级写成数据表后，顺序本身就是优先级，必须有测试钉住。"""

    def test_rule_order_is_the_priority(self):
        self.assertEqual([r.name for r in vf.ASSESS_RULES],
                         ["A1", "A2", "A2b", "A3", "A4", "A5", "A6", "A7", "A8"])

    def test_a2_precedes_a2b(self):
        """全部可写句柄都无名时，即使含 unknown 也不该升 RISKY。"""
        names = [r.name for r in vf.ASSESS_RULES]
        self.assertLess(names.index("A2"), names.index("A2b"))

    def test_every_rule_is_well_formed(self):
        for r in vf.ASSESS_RULES:
            self.assertIn(r.level, vf.LEVEL_ORDER, r.name)
            self.assertTrue(r.reason.strip(), r.name)
            self.assertTrue(r.condition.strip(), r.name)

    def test_fallback_level_is_caution(self):
        self.assertEqual(vf.FALLBACK_LEVEL, "CAUTION")

    def test_rule_table_matches_documented_priorities(self):
        """受保护进程必须最先判，四格在系统辅助之前。"""
        names = [r.name for r in vf.ASSESS_RULES]
        self.assertEqual(names[0], "A1")
        self.assertLess(names.index("A4"), names.index("A6"))


class _Args:
    """给 cmd_watch 用的最小参数对象（它只读 `volume`）。"""

    def __init__(self, volume=None):
        self.volume = volume


if __name__ == "__main__":
    unittest.main(verbosity=2)
