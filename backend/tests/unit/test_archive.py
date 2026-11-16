"""档案的渲染、解析往返与写入方式。"""
from pathlib import Path

import pytest

from app.services import archive
from app.services.archive import ArchiveError, FileLine, HistoryLine, ProductDoc, ProductLine, ProjectDoc
from app.services.nas import LocalFs, NasPaths

SET = ProductDoc("P2026-0012-007", "set", "中秋礼盒A", "P2026-0012", "套装/中秋礼盒A.jpg", "a02b" * 16,
    (("P2026-0012-002", "ai"), ("P2026-0012-003", "ai"), ("P2026-0012-004", "manual")),
    (("品类", "茶具套装", "ai"), ("材质", "陶瓷、胡桃木", "manual"), ("主色", "", "ai_confirmed")), "一段\n多行的“描述”", "qwen-vl", "2026-11-12")
ITEM = ProductDoc("P2026-0012-002", "item", "酒瓶", "P2026-0012", "单品/酒瓶.jpg", "c71e" * 16,
    (("P2026-0012-007", ""), ("P2026-0012-008", "")), (), "", "", "2026-11-12", status="voided")
PROJECT = ProjectDoc("P2026-0012", "company", "中秋礼盒", "泸州老窖", "酒业", True, "张三", "李四", "王五", "2026-11-12", "2026-11-13",
    (FileLine("01定稿文件/礼盒A  展开图_终稿.ai", 1288490188, "9f3a" * 16), FileLine("05 详情页/主视觉.jpg", 8 * 1024 * 1024, "c71e" * 16), FileLine("说明.txt", 0, "e3b0" * 16)),
    (ProductLine("P2026-0012-001", "item", "单品/定制 酒杯.jpg", ()), ProductLine("P2026-0012-002", "item", "单品/酒瓶.jpg", ("007",)),
     ProductLine("P2026-0012-007", "set", "套装/中秋礼盒A.jpg", ("002", "003", "004"))),
    (HistoryLine("单品/酒瓶.jpg", "_历史版本/酒瓶_20261113.jpg", "aa11" * 16, "replaced"),))


@pytest.mark.parametrize("doc", [SET, ITEM])
def test_product_archive_round_trip(doc: ProductDoc) -> None:
    text = archive.render_product(doc)
    assert archive.parse_product(text) == doc
    assert text.rstrip().endswith('"结束标记": "完"\n}')
    assert archive.render_product(archive.parse_product(text)) == text


def test_product_archive_follows_documented_shape() -> None:
    import json
    body = json.loads(archive.render_product(SET))
    assert list(body) == ["格式版本", "商品编号", "商品类型", "展示名", "立项编号", "主图相对路径", "主图指纹", "包含单品", "标签", "标签来源",
                          "自由描述", "识图模型", "入库时间", "状态", "结束标记"]
    assert body["格式版本"] == 2 and body["商品类型"] == "套装" and body["状态"] == "有效"
    assert body["包含单品"][2] == {"商品编号": "P2026-0012-004", "来源": "人工补充"} and body["包含单品"][0]["来源"] == "AI推荐"
    assert body["标签来源"] == {"品类": "AI", "材质": "人工", "主色": "AI已确认"}
    item = json.loads(archive.render_product(ITEM))
    assert item["所属套装"] == ["P2026-0012-007", "P2026-0012-008"] and "包含单品" not in item and item["状态"] == "已作废"


def test_project_archive_round_trip_and_layout() -> None:
    text = archive.render_project(PROJECT)
    assert archive.parse_project(text) == PROJECT
    lines = text.split("\n")
    assert lines[0] == "立项编号：P2026-0012   编号来源：公司立项编号"
    assert lines[2] == "企业：泸州老窖   行业：酒业   中标：是"
    assert lines[3] == "上传人：张三   初审：李四   终审：王五   入库时间：2026-11-12" and lines[4] == "最后更新：2026-11-13"
    assert "【文件清单】" in lines and "【商品】" in lines and "【历史版本】" in lines and lines[-2] == "结束标记：完"
    assert any(line.startswith("01定稿文件/礼盒A  展开图_终稿.ai") and "1.2GB(1288490188)" in line and line.endswith("指纹 " + "9f3a" * 16) for line in lines)
    assert any(line.startswith("P2026-0012-007  套装  套装/中秋礼盒A.jpg") and line.endswith("包含单品：002、003、004") for line in lines)
    assert "P2026-0012-001  单品  单品/定制 酒杯.jpg" in lines
    empty = ProjectDoc("H2025-0001", "system_h", "旧项目", "茅台", "", False, "甲", "乙", "丙", "2026-01-01", "2026-01-01")
    rendered = archive.render_project(empty)
    assert "编号来源：系统补发" in rendered and "中标：否" in rendered and rendered.count("（无）") == 3
    assert archive.parse_project(rendered) == empty


def test_archives_without_end_marker_are_damaged() -> None:
    product, project = archive.render_product(SET), archive.render_project(PROJECT)
    for broken in (product[: len(product) // 2], product.replace('"结束标记": "完"', '"结束标记": ""'), "[]"):
        with pytest.raises(ArchiveError):
            archive.parse_product(broken)
    for broken in (project[: len(project) // 2], project.replace("结束标记：完", ""), ""):
        with pytest.raises(ArchiveError):
            archive.parse_project(broken)
    with pytest.raises(ArchiveError):
        archive.parse_project(project.replace("【商品】", "【别的】"))


def test_human_size_and_archive_names() -> None:
    assert [archive.human_size(value) for value in (0, 512, 1024, 8 * 1024 * 1024, 1288490188)] == ["0B", "512B", "1KB", "8MB", "1.2GB"]
    assert archive.archive_name("item", "酒瓶.jpg", False) == "单品档案/酒瓶.json"
    assert archive.archive_name("set", "礼盒.png", True) == "套装档案/礼盒.png.json"


def test_write_uses_temporary_name_verifies_and_skips_identical(tmp_path: Path) -> None:
    fs = LocalFs(tmp_path, {"formal": "write", "temp": "write"}, NasPaths())
    path = "设计资产/档案.json"
    assert archive.write_text(fs, path, "第一版") is True
    assert archive.read_text(fs, path) == "第一版" and fs.list("设计资产") == ["档案.json"]
    assert archive.write_text(fs, path, "第一版") is False
    # 上次中断留下的临时文件不影响重写，也不会残留。
    with fs.open_write(path + ".写入中") as stream:
        stream.write("半截".encode())
    assert archive.write_text(fs, path, "第二版") is True
    assert archive.read_text(fs, path) == "第二版" and fs.list("设计资产") == ["档案.json"]
