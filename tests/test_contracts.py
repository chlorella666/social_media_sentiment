"""扩展接口 schema 契约检查（1.6 强制验收项）。

运行：python tests/test_contracts.py
覆盖两类扩展点：
1. 渠道注册表契约：每个注册渠道必须满足 ChannelAdapter 接口（元数据完整、
   info() 与属性一致、collect 签名合法、id 唯一且可解析）；
2. 领域 schema 契约：app/domains/*.json 必须满足 DomainSchema 结构
   （必填字段、id 唯一、keywords 非空、与 loader 往返一致）。
新增渠道/领域若不满足契约，回归直接 FAIL。
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.base import ChannelAdapter
from app.channels.registry import CHANNELS, get_channel, list_channel_infos
from app.core.models import DomainSchema
from app.domains import loader

REQUIRED_INFO_KEYS = {
    "id", "name", "auth_required", "applicability", "description", "demo",
}
REQUIRED_DOMAIN_KEYS = {"domain_id", "domain_name", "dimensions"}
REQUIRED_DIM_KEYS = {"id", "name", "keywords"}


def test_channel_registry_contract() -> None:
    infos = list_channel_infos()
    assert infos, "渠道注册表为空"
    ids = [i["id"] for i in infos]
    assert len(ids) == len(set(ids)), f"渠道 id 重复：{ids}"
    assert set(ids) == set(CHANNELS.keys()), "list_channel_infos 与注册表不一致"
    for info in infos:
        assert REQUIRED_INFO_KEYS <= set(info.keys()), f"渠道 {info.get('id')} 元数据缺字段"
        assert info["id"] and info["name"], f"渠道 {info.get('id')} id/name 为空"
        assert isinstance(info["auth_required"], bool)
        adapter = get_channel(info["id"])
        assert isinstance(adapter, ChannelAdapter), f"{info['id']} 不是 ChannelAdapter"
        assert adapter.id == info["id"] and adapter.name == info["name"]
        assert adapter.info() == info, f"{info['id']} info() 与属性不一致"
        sig = inspect.signature(adapter.collect)
        assert "plan" in sig.parameters, f"{info['id']}.collect 缺少 plan 参数"
    print(f"✓ 渠道注册表契约：{len(infos)} 个渠道全部满足接口（元数据/info/collect）")


def test_domain_schema_contract() -> None:
    files = sorted((loader.DOMAINS_DIR).glob("*.json"))
    assert files, "app/domains 无预置 schema"
    domain_ids = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert REQUIRED_DOMAIN_KEYS <= set(raw.keys()), f"{path.name} 缺顶层字段"
        schema = DomainSchema.model_validate(raw)
        assert schema.domain_id == path.stem, f"{path.name} domain_id 与文件名不一致"
        assert schema.domain_name, f"{path.name} domain_name 为空"
        assert schema.dimensions, f"{path.name} dimensions 为空"
        dim_ids = []
        for dim in schema.dimensions:
            assert REQUIRED_DIM_KEYS <= set(dim.model_dump().keys()), f"{path.name} 维度缺字段"
            assert dim.id and dim.name, f"{path.name} 维度 id/name 为空"
            assert dim.keywords, f"{path.name} 维度 {dim.id} keywords 为空"
            assert all(isinstance(k, str) and k for k in dim.keywords), f"{path.name} 维度 {dim.id} keywords 含空项"
            dim_ids.append(dim.id)
            sub_ids = [s.id for s in dim.sub_dimensions]
            assert len(sub_ids) == len(set(sub_ids)), f"{path.name} 子维度 id 重复"
            assert all(s.id and s.name and s.keywords for s in dim.sub_dimensions), f"{path.name} 子维度缺字段"
        assert len(dim_ids) == len(set(dim_ids)), f"{path.name} 维度 id 重复"
        domain_ids.append(schema.domain_id)
    assert len(domain_ids) == len(set(domain_ids)), "domain_id 全局重复"
    listed = [d["id"] for d in loader.list_domains()]
    assert sorted(listed) == sorted(domain_ids), "list_domains 与文件不一致"
    for did in domain_ids:
        schema = loader.load_domain(did)
        assert schema.domain_id == did, f"load_domain({did}) 往返不一致"
    print(f"✓ 领域 schema 契约：{len(files)} 个领域全部满足 DomainSchema 结构且可加载")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    test_channel_registry_contract()
    test_domain_schema_contract()
    print("扩展接口 schema 契约检查通过 ✅")


if __name__ == "__main__":
    main()
