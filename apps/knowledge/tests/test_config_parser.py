"""
apps.knowledge.parsers.config_parser 单元测试 —— Config（YAML/JSON/INI/env）解析器

覆盖范围：
- JSON 文件解析 → KV 展平输出（_flatten）
- YAML 文件解析 → KV 展平输出（模拟 yaml 模块）
- YAML 模块未安装 / JSON 解析失败 → 保留原文
- 非 YAML/JSON 后缀（INI/TXT）→ 保留原文
- 文件不存在 → 空列表
- _flatten 对 dict/list/标量的展平规则

用纯 pytest + tmp_path + monkeypatch（不依赖 DB，不依赖 PyYAML 安装状态）。
"""
import sys
import types
from unittest.mock import MagicMock

import pytest

from apps.knowledge.parsers.config_parser import ConfigParser, _flatten


@pytest.mark.unit
def test_parse_json_flattens_kv(tmp_path):
    """JSON 配置应展平为 KV 文本（嵌套 key 用 . 连接）"""
    cfg_file = tmp_path / 'conf.json'
    cfg_file.write_text('{"server": {"port": 8080}, "name": "demo"}', encoding='utf-8')

    blocks = ConfigParser().parse(str(cfg_file))

    assert len(blocks) == 1
    b = blocks[0]
    assert b['type'] == 'config'
    assert b['section_path'] == 'config'
    assert 'server.port = 8080' in b['content']
    assert 'name = demo' in b['content']


@pytest.mark.unit
def test_parse_yaml_flattens_kv(tmp_path, monkeypatch):
    """YAML 配置应展平为 KV 文本"""
    fake_yaml = types.ModuleType('yaml')
    fake_yaml.safe_load = MagicMock(return_value={'server': {'port': 8080}})
    monkeypatch.setitem(sys.modules, 'yaml', fake_yaml)

    cfg_file = tmp_path / 'conf.yaml'
    cfg_file.write_text('server:\n  port: 8080\n', encoding='utf-8')

    blocks = ConfigParser().parse(str(cfg_file))

    assert 'server.port = 8080' in blocks[0]['content']


@pytest.mark.unit
def test_parse_yaml_not_installed_keeps_raw_text(tmp_path, monkeypatch):
    """yaml 模块不可用时保留原文"""
    monkeypatch.setitem(sys.modules, 'yaml', None)

    cfg_file = tmp_path / 'conf.yml'
    raw = 'key: value\nother: 1\n'
    cfg_file.write_text(raw, encoding='utf-8')

    blocks = ConfigParser().parse(str(cfg_file))

    assert blocks[0]['content'] == raw


@pytest.mark.unit
def test_parse_invalid_json_keeps_raw_text(tmp_path):
    """JSON 解析失败时保留原文"""
    cfg_file = tmp_path / 'bad.json'
    raw = 'not a { json'
    cfg_file.write_text(raw, encoding='utf-8')

    blocks = ConfigParser().parse(str(cfg_file))

    assert blocks[0]['content'] == raw


@pytest.mark.unit
def test_parse_ini_keeps_raw_text(tmp_path):
    """INI 等非 YAML/JSON 配置保留原文"""
    cfg_file = tmp_path / 'app.ini'
    raw = '[section]\nkey = value\n'
    cfg_file.write_text(raw, encoding='utf-8')

    blocks = ConfigParser().parse(str(cfg_file))

    assert blocks[0]['content'] == raw


@pytest.mark.unit
def test_parse_missing_file_returns_empty_list(tmp_path):
    """文件不存在时返回空列表"""
    assert ConfigParser().parse(str(tmp_path / 'none.conf')) == []


@pytest.mark.unit
def test_flatten_nested_list_and_scalar():
    """_flatten 对 list 用索引、标量直接输出"""
    assert _flatten({'a': [1, 2], 'b': 'x'}) == 'a[0] = 1\na[1] = 2\nb = x'
    assert _flatten('plain') == ' = plain'
    assert _flatten({'a': {'b': {'c': 1}}}) == 'a.b.c = 1'
