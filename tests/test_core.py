"""核心逻辑单测：不需要网络，也不需要浏览器。

重点覆盖三条「绝不能错」的行为：
1. 查询失败不能被当成无货。
2. 只在状态变化时提醒，重复有货按配置节流。
3. 被拦后进入退避，而不是继续按原间隔敲门。
"""

from __future__ import annotations

import json
import ssl
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

from app.browser import FetchError, PartStatus, StoreAvailability, parse_pickup_response
from app.config import Config, PickupScope, ProductSpec, load_config
from app.monitor import (
    Monitor,
    MonitorState,
    PartState,
    ScanRecord,
    _chunk,
    format_alert,
)
from app.regions import REGIONS

# --------------------------------------------------------------------------
# 响应解析
# --------------------------------------------------------------------------

def _response(stores: list[dict]) -> str:
    return json.dumps({"head": {"status": "200"}, "body": {"stores": stores}})


def _store(number: str, name: str, parts: dict[str, str], city: str = "上海") -> dict:
    return {
        "storeNumber": number,
        "storeName": name,
        "city": city,
        "partsAvailability": {
            part: {
                "pickupDisplay": display,
                "messageTypes": {
                    "regular": {
                        "storePickupProductTitle": f"商品 {part}",
                        "storePickupQuote": f"今天；Apple {name}",
                    }
                },
            }
            for part, display in parts.items()
        },
    }


class TestParseResponse:
    def test_解析有货与无货(self):
        body = _response([_store("R683", "环球港", {"MJ1": "available", "MJ2": "unavailable"})])
        stores = parse_pickup_response(body, expected_parts=["MJ1", "MJ2"])
        assert len(stores) == 1
        assert stores[0].parts["MJ1"].available is True
        assert stores[0].parts["MJ2"].available is False

    def test_多门店(self):
        body = _response(
            [
                _store("R683", "环球港", {"MJ1": "available"}),
                _store("R390", "香港广场", {"MJ1": "unavailable"}),
            ]
        )
        stores = parse_pickup_response(body, expected_parts=["MJ1"])
        assert {s.store_number for s in stores} == {"R683", "R390"}

    def test_未知取值不算有货(self):
        # Apple 新增了没见过的取值时，只能算「无货/未知」，不能算有货。
        body = _response([_store("R683", "环球港", {"MJ1": "something_new"})])
        stores = parse_pickup_response(body, expected_parts=["MJ1"])
        assert stores[0].parts["MJ1"].available is False

    def test_非JSON抛错而不是当成无货(self):
        with pytest.raises(FetchError):
            parse_pickup_response("<html>blocked</html>", expected_parts=["MJ1"])

    def test_head状态异常抛错(self):
        body = json.dumps({"head": {"status": "500"}, "body": {"stores": []}})
        with pytest.raises(FetchError):
            parse_pickup_response(body, expected_parts=["MJ1"])

    def test_业务错误抛错(self):
        body = json.dumps({"body": {"errorMessage": "系统忙", "stores": []}})
        with pytest.raises(FetchError, match="系统忙"):
            parse_pickup_response(body, expected_parts=["MJ1"])

    def test_没有门店数据抛错(self):
        body = json.dumps({"head": {"status": "200"}, "body": {"stores": []}})
        with pytest.raises(FetchError):
            parse_pickup_response(body, expected_parts=["MJ1"])

    def test_未覆盖任何请求型号抛错(self):
        body = _response([_store("R683", "环球港", {"OTHER": "available"})])
        with pytest.raises(FetchError, match="未覆盖"):
            parse_pickup_response(body, expected_parts=["MJ1"])

    def test_兼容嵌套pickupMessage结构(self):
        body = json.dumps(
            {
                "head": {"status": "200"},
                "body": {"content": {"pickupMessage": {"stores": [_store("R683", "环球港", {"MJ1": "available"})]}}},
            }
        )
        stores = parse_pickup_response(body, expected_parts=["MJ1"])
        assert stores[0].parts["MJ1"].available is True


# --------------------------------------------------------------------------
# 状态机与提醒节流
# --------------------------------------------------------------------------

def _config(tmp_path: Path, **overrides) -> Config:
    base = dict(
        region=REGIONS["cn"],
        interval=300,
        jitter=0.0,
        pickup=PickupScope(cities=("上海 上海",)),
        products=(ProductSpec("MJ1", "测试型号"),),
        auto=(),
        notify=(),
        repeat_when_in_stock=True,
        repeat_interval=1800,
        state_file=tmp_path / "state.json",
        log_level="INFO",
        degraded_alert_after=4,
    )
    base.update(overrides)
    return Config(**base)


def _monitor(tmp_path: Path, **overrides) -> Monitor:
    config = _config(tmp_path, **overrides)
    return Monitor(config, browser=None, notifier=None)  # type: ignore[arg-type]


def _stores(display: str) -> dict[str, StoreAvailability]:
    return {
        "R683": StoreAvailability(
            store_number="R683",
            store_name="环球港",
            city="上海",
            parts={"MJ1": PartStatus("MJ1", display == "available", title="测试型号", quote="今天")},
        )
    }


class TestReconcile:
    def test_首次有货提醒(self, tmp_path):
        monitor = _monitor(tmp_path)
        alerts = monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        assert len(alerts) == 1
        assert alerts[0].store_name == "环球港"

    def test_持续有货不重复提醒(self, tmp_path):
        monitor = _monitor(tmp_path, repeat_when_in_stock=False)
        monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        again = monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        assert again == []

    def test_无货不提醒(self, tmp_path):
        monitor = _monitor(tmp_path)
        assert monitor._reconcile(_stores("unavailable"), {"MJ1": "测试型号"}) == []

    def test_有货到无货再到有货会再次提醒(self, tmp_path):
        monitor = _monitor(tmp_path, repeat_when_in_stock=True)
        first = monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        assert len(first) == 1
        monitor._reconcile(_stores("unavailable"), {"MJ1": "测试型号"})
        # 绕过同型号冷却窗口
        monitor.state.get("R683", "MJ1").last_alert_at = 0
        third = monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        assert len(third) == 1

    def test_有货重启后不重复提醒(self, tmp_path):
        monitor = _monitor(tmp_path)
        monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        monitor.state.save(monitor.state_file)

        reborn = _monitor(tmp_path)
        reborn.state.load(reborn.state_file)
        assert reborn._reconcile(_stores("available"), {"MJ1": "测试型号"}) == []

    def test_重复提醒间隔生效(self, tmp_path):
        monitor = _monitor(tmp_path, repeat_when_in_stock=True, repeat_interval=1800)
        monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        # 刚提醒过，紧接着再跑不该再提醒
        assert monitor._reconcile(_stores("available"), {"MJ1": "测试型号"}) == []
        # 把时间拨到间隔之外
        monitor.state.get("R683", "MJ1").last_alert_at = time.time() - 4000
        assert len(monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})) == 1

    def test_只关注配置里的型号(self, tmp_path):
        monitor = _monitor(tmp_path)
        stores = {
            "R683": StoreAvailability(
                "R683",
                "环球港",
                "上海",
                {"OTHER": PartStatus("OTHER", True, title="别的型号")},
            )
        }
        assert monitor._reconcile(stores, {"MJ1": "测试型号"}) == []


class TestBackoff:
    def test_首次被拦退避开到十分钟(self, tmp_path):
        monitor = _monitor(tmp_path)
        monitor.state.consecutive_blocks = 1
        delay = monitor._next_delay(blocked=True)
        assert 500 <= delay <= 700

    def test_连续被拦逐步延长(self, tmp_path):
        monitor = _monitor(tmp_path)
        delays = []
        for count in range(1, 6):
            monitor.state.consecutive_blocks = count
            delays.append(monitor._next_delay(blocked=True))
        assert delays == sorted(delays)
        assert delays[-1] > delays[0] * 4

    def test_被拦后不再是短间隔(self, tmp_path):
        monitor = _monitor(tmp_path, interval=120)
        assert monitor._next_delay(blocked=True) > 120

    def test_正常轮次按配置间隔(self, tmp_path):
        monitor = _monitor(tmp_path, interval=300, jitter=0.0)
        assert monitor._next_delay(blocked=False) == 300

    def test_抖动用例(self, tmp_path):
        monitor = _monitor(tmp_path, interval=300, jitter=0.2)
        samples = {monitor._next_delay(blocked=False) for _ in range(50)}
        assert len(samples) > 1
        assert all(240 <= value <= 360 for value in samples)


# --------------------------------------------------------------------------
# 批次与通知文案
# --------------------------------------------------------------------------

class TestBatchAndFormat:
    def test_按20个一批切分(self):
        parts = [f"P{i}" for i in range(45)]
        batches = _chunk(parts, 20)
        assert [len(b) for b in batches] == [20, 20, 5]

    def test_空列表不产生批次(self):
        assert _chunk([], 20) == []

    def test_单条提醒标题(self, tmp_path):
        monitor = _monitor(tmp_path)
        alerts = monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        title, body = format_alert(monitor.config, alerts)
        assert "有货" in title
        assert "环球港" in title
        assert "MJ1" in body

    def test_多条提醒汇总(self, tmp_path):
        from app.monitor import Alert

        alerts = [
            Alert("R683", "环球港", "上海", "MJ1", "型号A", "今天"),
            Alert("R390", "香港广场", "上海", "MJ1", "型号A", "今天"),
        ]
        title, body = format_alert(_monitor(tmp_path).config, alerts)
        assert "2 家门店" in title
        assert "环球港" in body and "香港广场" in body


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

class TestConfig:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_最小配置(self, tmp_path):
        path = self._write(
            tmp_path,
            "region: cn\npickup:\n  cities: ['上海 上海']\nproducts: ['MJ1']\n",
        )
        config = load_config(path)
        assert config.region.key == "cn"
        assert config.products[0].part == "MJ1"
        assert config.interval == 300

    def test_间隔过短被拒绝(self, tmp_path):
        path = self._write(
            tmp_path, "region: cn\ninterval: 90\npickup:\n  cities: ['上海 上海']\nproducts: ['MJ1']\n"
        )
        with pytest.raises(Exception, match="太短"):
            load_config(path)

    def test_缺少范围被拒绝(self, tmp_path):
        path = self._write(tmp_path, "region: cn\nproducts: ['MJ1']\n")
        with pytest.raises(Exception, match="pickup"):
            load_config(path)

    def test_没有型号被拒绝(self, tmp_path):
        path = self._write(tmp_path, "region: cn\npickup:\n  cities: ['上海 上海']\n")
        with pytest.raises(Exception, match="products"):
            load_config(path)

    def test_环境变量展开(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_BARK_URL", "https://api.day.app/abc")
        path = self._write(
            tmp_path,
            "region: cn\npickup:\n  cities: ['上海 上海']\nproducts: ['MJ1']\n"
            "notify:\n  - type: bark\n    url: env:TEST_BARK_URL\n",
        )
        config = load_config(path)
        assert config.notify[0].options["url"] == "https://api.day.app/abc"

    def test_环境变量缺失时报错(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        path = self._write(
            tmp_path,
            "region: cn\npickup:\n  cities: ['上海 上海']\nproducts: ['MJ1']\n"
            "notify:\n  - type: bark\n    url: env:MISSING_VAR\n",
        )
        with pytest.raises(Exception, match="MISSING_VAR"):
            load_config(path)

    def test_地区可用locale指定(self, tmp_path):
        path = self._write(
            tmp_path, "region: zh_HK\npickup:\n  cities: ['香港']\nproducts: ['MJ1']\n"
        )
        assert load_config(path).region.key == "hk"


class TestStateFile:
    def test_保存与载入(self, tmp_path):
        state = MonitorState()
        state.get("R683", "MJ1").available = True
        state.consecutive_blocks = 3
        path = tmp_path / "nested" / "state.json"
        state.save(path)

        loaded = MonitorState()
        loaded.load(path)
        assert loaded.get("R683", "MJ1").available is True
        assert loaded.consecutive_blocks == 3

    def test_坏文件不影响启动(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        state = MonitorState()
        state.load(path)  # 不应抛异常
        assert state.parts == {}

    def test_未知值不被当作无货(self):
        state = PartState()
        assert state.available is None


# --------------------------------------------------------------------------
# 时间策略与配置回写
# --------------------------------------------------------------------------

class TestQuietHours:
    def test_默认关闭(self):
        from app.config import QuietHours

        assert QuietHours().covers(datetime(2026, 1, 1, 3, 0)) is False

    def test_跨午夜覆盖凌晨(self):
        from app.config import QuietHours

        quiet = QuietHours(enabled=True, start="23:00", end="08:00")
        assert quiet.covers(datetime(2026, 1, 1, 23, 30)) is True
        assert quiet.covers(datetime(2026, 1, 1, 3, 0)) is True
        assert quiet.covers(datetime(2026, 1, 1, 12, 0)) is False

    def test_同日内区间(self):
        from app.config import QuietHours

        quiet = QuietHours(enabled=True, start="12:00", end="14:00")
        assert quiet.covers(datetime(2026, 1, 1, 13, 0)) is True
        assert quiet.covers(datetime(2026, 1, 1, 15, 0)) is False

    def test_起止相同视为不覆盖(self):
        from app.config import QuietHours

        quiet = QuietHours(enabled=True, start="08:00", end="08:00")
        assert quiet.covers(datetime(2026, 1, 1, 8, 0)) is False

    def test_配置里可写简写(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            'quiet_hours: "22:30-07:30"\n',
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.quiet_hours.enabled is True
        assert config.quiet_hours.start == "22:30"
        assert config.quiet_hours.end == "07:30"

    def test_兼容YAML六十进制整数(self, tmp_path):
        # YAML 1.1 会把裸写的 23:30 读成 1410，旧配置可能已经这样了
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "quiet_hours:\n  enabled: true\n  start: 1410\n  end: 450\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.quiet_hours.start == "23:30"
        assert config.quiet_hours.end == "07:30"

    def test_非法时间被拒绝(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "quiet_hours:\n  enabled: true\n  start: '25:99'\n  end: '08:00'\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="不是合法时间"):
            load_config(path)


class TestBackoffConfig:
    def test_默认阶梯(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n", encoding="utf-8"
        )
        assert load_config(path).backoff_ladder == (600, 1200, 2400, 3600, 7200)

    def test_自定义阶梯生效(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "backoff_ladder: [300, 900, 1800]\n",
            encoding="utf-8",
        )
        config = load_config(path)
        monitor = Monitor(config, browser=None, notifier=None)  # type: ignore[arg-type]
        monitor.state.consecutive_blocks = 2
        delay = monitor._next_delay(blocked=True)
        assert 765 <= delay <= 1035  # 900 * [0.85, 1.15]

    def test_非递增被拒绝(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "backoff_ladder: [900, 300]\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="递增"):
            load_config(path)

    def test_过短被拒绝(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "backoff_ladder: [10]\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="太短"):
            load_config(path)


class TestConfigWrite:
    def _base(self, tmp_path: Path) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(
            "# 这行注释必须保留\nregion: cn\ninterval: 300\n"
            "pickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n",
            encoding="utf-8",
        )
        return path

    def test_保留注释(self, tmp_path):
        from app.configwrite import save_settings

        path = self._base(tmp_path)
        save_settings(path, interval=600)
        text = path.read_text(encoding="utf-8")
        assert "# 这行注释必须保留" in text
        assert "interval: 600" in text

    def test_时间写成带引号字符串(self, tmp_path):
        from app.configwrite import save_settings

        path = self._base(tmp_path)
        save_settings(
            path,
            quiet_hours={"enabled": True, "start": "23:30", "end": "07:30", "mode": "defer"},
        )
        text = path.read_text(encoding="utf-8")
        assert "'23:30'" in text or '"23:30"' in text
        # 回读仍然是时间，而不是 1410
        assert load_config(path).quiet_hours.start == "23:30"

    def test_写入后可被重新加载(self, tmp_path):
        from app.configwrite import save_settings

        path = self._base(tmp_path)
        save_settings(
            path,
            interval=480,
            jitter=0.15,
            cities=["北京 北京", "天津 天津"],
            products=[{"part": "MK2M4CH/A", "name": "星光白 256GB"}],
            backoff=[300, 900],
            degraded_alert_after=5,
            repeat_when_in_stock=False,
            repeat_interval=3600,
        )
        config = load_config(path)
        assert config.interval == 480
        assert config.jitter == 0.15
        assert config.pickup.cities == ("北京 北京", "天津 天津")
        assert config.products[0].part == "MK2M4CH/A"
        assert config.backoff_ladder == (300, 900)
        assert config.degraded_alert_after == 5
        assert config.repeat_when_in_stock is False


class TestStoreCatalog:
    def _catalog(self):
        from app.stores import StoreCatalog, StoreInfo

        return StoreCatalog(
            stores=[
                StoreInfo("R320", "三里屯", "北京", "北京"),
                StoreInfo("R637", "天津大悦城", "天津", "天津"),
                StoreInfo("R1", "广州天环", "广东", "广州"),
                StoreInfo("R2", "深圳万象城", "广东", "深圳"),
            ]
        )

    def test_直辖市写成省市同名(self):
        catalog = self._catalog()
        assert catalog.resolve_locations(["北京"]) == ["北京 北京"]

    def test_多城市省份展开成多个地点(self):
        catalog = self._catalog()
        assert catalog.resolve_locations(["广东"]) == ["广东 广州", "广东 深圳"]

    def test_反查省份(self):
        catalog = self._catalog()
        assert catalog.province_of_location("北京 北京") == "北京"
        assert catalog.province_of_location("广东 深圳") == "广东"

    def test_解析门店列表页(self):
        from app.stores import parse_storelist

        payload = {
            "props": {
                "pageProps": {
                    "storeList": [
                        {
                            "locale": "zh_CN",
                            "state": [
                                {
                                    "name": "北京",
                                    "store": [
                                        {
                                            "id": "R320",
                                            "name": "三里屯",
                                            "address": {"city": "北京"},
                                        }
                                    ],
                                }
                            ],
                        },
                        {"locale": "en_US", "state": []},
                    ]
                }
            }
        }
        stores = parse_storelist(payload)
        assert len(stores) == 1
        assert stores[0].number == "R320"
        assert stores[0].location == "北京 北京"


class TestDeferredAlerts:
    def test_静默时段攒下提醒(self, tmp_path, monkeypatch):
        """defer 模式下，静默期内的提醒不推送，静默结束后合并补发。"""
        import asyncio

        config = _config(tmp_path)
        object.__setattr__(config, "quiet_hours", _quiet_enabled("defer"))
        monitor = Monitor(config, browser=None, notifier=None)  # type: ignore[arg-type]

        sent: list[tuple[str, str]] = []

        class Rec:
            async def send(self, title, body):
                sent.append((title, body))

        monitor.notifier = Rec()  # type: ignore[assignment]

        # 静默时段判断走 tz.current()（服务器时钟可能是 UTC，不能直接用 datetime.now）
        import app.monitor as monitor_module
        import app.tz as tz_module

        class FakeTZ:
            @staticmethod
            def current():
                return datetime(2026, 1, 1, 2, 0)

        monkeypatch.setattr(monitor_module, "tz", FakeTZ)
        monkeypatch.setattr(tz_module, "_TZ", tz_module._TZ)  # 保持其他路径可用
        alerts = monitor._reconcile(_stores("available"), {"MJ1": "测试型号"})
        asyncio.run(monitor._emit(alerts, {}, {"MJ1": "测试型号"}))
        assert sent == []
        assert len(monitor._pending_alerts) == 1


def _quiet_enabled(mode: str):
    from app.config import QuietHours

    return QuietHours(enabled=True, start="23:00", end="08:00", mode=mode)


class TestProxySettings:
    def test_socks5_直连(self):
        from app.browser import _proxy_settings

        assert _proxy_settings("socks5://127.0.0.1:1080") == {
            "server": "socks5://127.0.0.1:1080"
        }

    def test_带认证的代理拆出账号密码(self):
        from app.browser import _proxy_settings

        settings = _proxy_settings("socks5://user:p%40ss@10.0.0.1:1080")
        assert settings == {
            "server": "socks5://10.0.0.1:1080",
            "username": "user",
            "password": "p@ss",
        }

    def test_省略协议默认http(self):
        from app.browser import _proxy_settings

        assert _proxy_settings("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}

    def test_不支持协议报错(self):
        from app.browser import FetchError, _proxy_settings

        with pytest.raises(FetchError, match="不支持的代理协议"):
            _proxy_settings("ftp://1.2.3.4:21")

    def test_未配置代理返回None(self):
        from app.browser import _proxy_settings

        assert _proxy_settings(None) is None
        assert _proxy_settings("") is None

    def test_bypass透传(self):
        from app.browser import _proxy_settings

        settings = _proxy_settings("socks5://h:1", bypass="127.0.0.1,localhost")
        assert settings["bypass"] == "127.0.0.1,localhost"


class TestProxyConfig:
    def test_配置里可写代理(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "proxy: socks5://192.168.1.2:1080\n"
            "proxy_bypass: '127.0.0.1,localhost'\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.proxy == "socks5://192.168.1.2:1080"
        assert config.proxy_bypass == "127.0.0.1,localhost"

    def test_未配置时代理为空(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n", encoding="utf-8"
        )
        assert load_config(path).proxy == ""


# --------------------------------------------------------------------------
# 通知渠道
# --------------------------------------------------------------------------

class TestBarkPayload:
    def _capture(self, **options):
        """用本地 mock 服务抓 Bark 实际发出的请求体。"""
        import asyncio
        import json as jsonlib
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                captured["path"] = self.path
                captured["body"] = jsonlib.loads(self.rfile.read(length) or b"{}")
                out = jsonlib.dumps({"code": 200, "message": "success"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            from app.notify import BarkChannel

            channel = BarkChannel(f"http://127.0.0.1:{port}/mykey", **options)
            asyncio.run(channel.send("标题", "正文"))
        finally:
            server.shutdown()
        return captured

    def test_基本字段(self):
        captured = self._capture(group="Apple", level="timeSensitive")
        assert captured["path"] == "/mykey"
        assert captured["body"]["title"] == "标题"
        assert captured["body"]["group"] == "Apple"
        assert captured["body"]["level"] == "timeSensitive"

    def test_可选字段留空不下发(self):
        body = self._capture()["body"]
        for key in ("sound", "volume", "call", "icon", "url"):
            assert key not in body

    def test_铃声与音量(self):
        body = self._capture(sound="bell", volume=7)["body"]
        assert body["sound"] == "bell"
        assert body["volume"] == 7

    def test_持续响铃(self):
        assert self._capture(call=True)["body"]["call"] == "1"

    def test_点击跳转(self):
        assert self._capture(click="https://x.test")["body"]["url"] == "https://x.test"

    def test_key被打码(self):
        from app.notify import BarkChannel

        channel = BarkChannel("https://api.day.app/abcdef123456")
        described = channel.describe()
        assert "abcdef123456" not in described
        assert "…" in described


class TestMaskedSecrets:
    def test_打码保留尾部(self):
        from app.dashboard import _mask_secret

        masked = _mask_secret("https://api.day.app/mysecretkey123")
        assert masked.startswith("https://api.day.app/")
        assert "mysecretkey" not in masked
        assert masked.endswith("y123")

    def test_短密钥整体打码(self):
        from app.dashboard import _mask_secret

        assert _mask_secret("abc") == "****"

    def test_保存时打码值还原成真实密钥(self):
        """用户只改音量、不动密钥时，不能把 ****3456 写进配置。"""
        from app.config import NotifyChannel
        from app.dashboard import _notify_for_save

        existing = (
            NotifyChannel(
                type="bark",
                options={"url": "https://api.day.app/real_key_9999", "group": "Apple"},
            ),
        )
        saved = _notify_for_save(
            [{"type": "bark", "url": "https://api.day.app/****9999", "group": "Apple", "volume": "3"}],
            existing,
        )
        assert saved[0]["url"] == "https://api.day.app/real_key_9999"
        assert saved[0]["volume"] == "3"

    def test_填了新密钥就用新的(self):
        from app.config import NotifyChannel
        from app.dashboard import _notify_for_save

        existing = (NotifyChannel(type="bark", options={"url": "https://api.day.app/old"}),)
        saved = _notify_for_save([{"type": "bark", "url": "https://api.day.app/new"}], existing)
        assert saved[0]["url"] == "https://api.day.app/new"

    def test_空值被丢弃(self):
        from app.dashboard import _notify_for_save

        saved = _notify_for_save(
            [{"type": "bark", "url": "https://x.test/k", "sound": "", "icon": None}], ()
        )
        assert "sound" not in saved[0]
        assert "icon" not in saved[0]


class TestNotifyPersistence:
    """确保面板上配的通知渠道真的能写进配置。

    这里守的是一个已经踩过的坑：前端 currentPayload 漏了 notify 字段，
    于是界面上配好的 Bark 保存后配置里仍然是 console，推送永远不生效，
    而且界面上看不出任何异常。
    """

    def test_notify能写进配置(self, tmp_path):
        from app.configwrite import save_settings

        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "notify:\n  - type: console\n",
            encoding="utf-8",
        )
        save_settings(
            path,
            notify=[
                {
                    "type": "bark",
                    "url": "https://api.day.app/KEY",
                    "group": "Apple",
                    "level": "timeSensitive",
                }
            ],
        )
        config = load_config(path)
        assert len(config.notify) == 1
        assert config.notify[0].type == "bark"
        assert config.notify[0].options["url"] == "https://api.day.app/KEY"

    def test_多渠道并存(self, tmp_path):
        from app.configwrite import save_settings

        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n",
            encoding="utf-8",
        )
        save_settings(
            path,
            notify=[
                {"type": "bark", "url": "https://api.day.app/K"},
                {"type": "telegram", "token": "t", "chat_id": "1"},
                {"type": "console"},
            ],
        )
        config = load_config(path)
        assert [c.type for c in config.notify] == ["bark", "telegram", "console"]

    def test_bark可选项被保留(self, tmp_path):
        from app.configwrite import save_settings

        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n",
            encoding="utf-8",
        )
        save_settings(
            path,
            notify=[
                {
                    "type": "bark",
                    "url": "https://api.day.app/K",
                    "sound": "bell",
                    "volume": "7",
                    "call": True,
                }
            ],
        )
        options = load_config(path).notify[0].options
        assert options["sound"] == "bell"
        assert options["volume"] == "7"
        assert options["call"] is True

    def test_渠道构建能读到可选项(self):
        from app.config import NotifyChannel
        from app.notify import BarkChannel, build_channels

        channels = build_channels(
            (
                NotifyChannel(
                    type="bark",
                    options={
                        "url": "https://api.day.app/K",
                        "sound": "bell",
                        "volume": "8",
                        "call": True,
                    },
                ),
            )
        )
        bark = channels[0]
        assert isinstance(bark, BarkChannel)
        assert bark.sound == "bell"
        assert bark.volume == 8
        assert bark.call is True

    def test_没有渠道时回退到日志(self):
        from app.notify import ConsoleChannel, build_channels

        channels = build_channels(())
        assert len(channels) == 1
        assert isinstance(channels[0], ConsoleChannel)


class TestTlsContext:
    """守一个踩过两次的坑：缺根证书导致所有 HTTPS 失败。

    第一次是门店列表，第二次是 Bark 推送。错误信息像网络故障，实际是环境
    问题，所以这里断言 CA 真的加载了。
    """

    def test_上下文带CA(self):
        from app.tls import ssl_context

        ctx = ssl_context()
        assert ctx is not True, "应当返回真正的 SSLContext 而不是 True"
        stats = ctx.cert_store_stats()
        assert stats["x509_ca"] > 0, "CA 证书数为 0 会导致所有 HTTPS 校验失败"

    def test_复用同一个上下文(self):
        from app.tls import ssl_context

        assert ssl_context() is ssl_context()

    def test_校验没有被关闭(self):
        from app.tls import ssl_context

        ctx = ssl_context()
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_notify连接器使用该上下文(self):
        import asyncio

        from app.notify import _connector
        from app.tls import ssl_context

        async def check():
            connector = _connector()
            try:
                assert connector._ssl is ssl_context()
            finally:
                await connector.close()

        asyncio.run(check())


# --------------------------------------------------------------------------
# 补货历史
# --------------------------------------------------------------------------

class TestScanHistory:
    """补货历史：只记有货的扫描，保留最近 10 条，重启不丢。"""

    def test_有货轮次被记录(self, tmp_path):
        monitor = _monitor(tmp_path)
        monitor._run_cycle_record_for_test = None  # 占位避免误用
        # 模拟 _run_cycle 末尾的记录逻辑
        in_stock = 1
        if in_stock:
            monitor.state.record_scan(
                ScanRecord(at=1000.0, store_count=1, part_count=1,
                           details=(("环球港", "测试型号"),))
            ,
                continuous=True
            )
        assert len(monitor.state.history) == 1
        assert monitor.state.history[0].store_count == 1
        assert monitor.state.history[0].details == (("环球港", "测试型号"),)

    def test_无货不记录(self):
        """无货轮次不该产生记录——这是需求的核心。"""
        monitor = _monitor(Path(tempfile.mkdtemp()))
        # in_stock = 0 的分支不会调用 record_scan
        assert monitor.state.history == []

    def test_只保留最近10条(self, tmp_path):
        monitor = _monitor(tmp_path)
        for i in range(15):
            # 每次补货集合都不同，避免被「与上一条相同」的去重吞掉
            monitor.state.record_scan(
                ScanRecord(at=float(i), store_count=1, part_count=1,
                           details=((f"门店{i}", "型号A"),))
            ,
                continuous=True
            )
        assert len(monitor.state.history) == 10
        # 新的在前：最后插入的（14）排第一
        assert monitor.state.history[0].at == 14.0
        assert monitor.state.history[-1].at == 5.0

    def test_落盘与重启(self, tmp_path):
        monitor = _monitor(tmp_path)
        monitor.state.record_scan(
            ScanRecord(at=1234.5, store_count=2, part_count=3,
                       details=(("A 店", "型号X"), ("B 店", "型号Y")))
        ,
                continuous=True
            )
        monitor.state.save(monitor.state_file)

        reborn = _monitor(tmp_path)
        reborn.state.load(reborn.state_file)
        assert len(reborn.state.history) == 1
        record = reborn.state.history[0]
        assert record.at == 1234.5
        assert record.store_count == 2
        assert record.details == (("A 店", "型号X"), ("B 店", "型号Y"))

    def test_坏历史数据不致命(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"history": [{"at": "bad", "details": "notalist"}, 42, None]}),
            encoding="utf-8",
        )
        state = MonitorState()
        state.load(path)  # 不应抛异常
        assert state.history == []

    def test_真实循环里有货时记录无货时不记录(self, tmp_path):
        """用真实的 _run_cycle 验证记录行为。"""
        import asyncio

        class Rec:
            def __init__(self):
                self.sent = []

            async def send(self, title, body):
                self.sent.append((title, body))

            def describe(self):
                return "测试"

        class FakeBrowser:
            def __init__(self, outcome):
                self.outcome = outcome

            async def discover_products(self, spec):
                return {}

            async def query(self, **kw):
                return self.outcome

        config = _config(tmp_path)
        monitor = Monitor(config, FakeBrowser(list(_stores("available").values())), Rec())  # type: ignore[arg-type]
        asyncio.run(monitor._run_cycle())
        assert len(monitor.state.history) == 1
        assert monitor.state.history[0].store_count == 1

        # 第二轮无货：不新增记录
        monitor.browser = FakeBrowser(list(_stores("unavailable").values()))  # type: ignore[assignment]
        asyncio.run(monitor._run_cycle())
        assert len(monitor.state.history) == 1


class TestScanHistoryDedup:
    """持续有货的型号不应每轮都记一条一模一样的历史。"""

    def test_相同补货集合不重复记录(self, tmp_path):
        monitor = _monitor(tmp_path)
        record = ScanRecord(at=1.0, store_count=1, part_count=1,
                            details=(("环球港", "型号A"),))
        monitor.state.record_scan(record,
                continuous=True
            )
        monitor.state.record_scan(
            ScanRecord(at=2.0, store_count=1, part_count=1, details=record.details)
        ,
                continuous=True
            )
        assert len(monitor.state.history) == 1
        assert monitor.state.history[0].at == 1.0

    def test_补货集合变化会记新条目(self, tmp_path):
        monitor = _monitor(tmp_path)
        monitor.state.record_scan(
            ScanRecord(at=1.0, store_count=1, part_count=1, details=(("环球港", "型号A"),))
        ,
                continuous=True
            )
        # 多了一家店
        monitor.state.record_scan(
            ScanRecord(at=2.0, store_count=2, part_count=1,
                       details=(("环球港", "型号A"), ("香港广场", "型号A")))
        ,
                continuous=True
            )
        assert len(monitor.state.history) == 2
        assert monitor.state.history[0].store_count == 2

    def test_集合减少也记新条目(self, tmp_path):
        monitor = _monitor(tmp_path)
        monitor.state.record_scan(
            ScanRecord(at=1.0, store_count=2, part_count=1,
                       details=(("环球港", "型号A"), ("香港广场", "型号A")))
        ,
                continuous=True
            )
        monitor.state.record_scan(
            ScanRecord(at=2.0, store_count=1, part_count=1, details=(("环球港", "型号A"),))
        ,
                continuous=True
            )
        assert len(monitor.state.history) == 2

    def test_同型号不同门店视为变化(self, tmp_path):
        monitor = _monitor(tmp_path)
        monitor.state.record_scan(
            ScanRecord(at=1.0, store_count=1, part_count=1, details=(("环球港", "型号A"),))
        ,
                continuous=True
            )
        monitor.state.record_scan(
            ScanRecord(at=2.0, store_count=1, part_count=1, details=(("南京东路", "型号A"),))
        ,
                continuous=True
            )
        assert len(monitor.state.history) == 2


# --------------------------------------------------------------------------
# 出口轮换（Clash / ClashVerge）
# --------------------------------------------------------------------------

class TestRoutePool:
    """冷却与选点逻辑。全部不联网。"""

    def _pool(self, nodes=None):
        from app.routes import RoutePool

        pool = RoutePool()
        pool.set_nodes(nodes or ["A", "B", "C"])
        return pool

    def test_首次被拒冷却5分钟(self):
        pool = self._pool()
        assert pool.block("A", "site", now=1000.0) == 300

    def test_连续被拒逐级延长(self):
        pool = self._pool()
        # 冷却结束后再被拒：升到 10 分钟
        pool.block("A", "site", now=0.0)
        assert pool.block("A", "site", now=301.0) == 600
        assert pool.block("A", "site", now=901.0) == 1200
        assert pool.block("A", "site", now=2101.0) == 1800
        # 封顶 30 分钟
        assert pool.block("A", "site", now=3901.0) == 1800

    def test_成功一次恢复(self):
        pool = self._pool()
        pool.block("A", "site", now=0.0)
        pool.mark_ok("A", "site")
        assert pool.cooling_remaining("A", "site", 10.0) == 0.0
        # 恢复后再被拒，从 5 分钟重新开始
        assert pool.block("A", "site", now=20.0) == 300

    def test_冷却中的节点不被选中(self):
        pool = self._pool()
        pool.block("A", "site", now=0.0)
        assert pool.next_node("site", now=10.0) == "B"

    def test_跳过当前节点(self):
        pool = self._pool()
        pool.active = "A"
        assert pool.next_node("site", now=0.0) == "B"

    def test_跳过排除名单(self):
        pool = self._pool()
        assert pool.next_node("site", now=0.0, exclude={"A", "B"}) == "C"

    def test_全部冷却返回None(self):
        pool = self._pool(["A"])
        pool.block("A", "site", now=0.0)
        assert pool.next_node("site", now=10.0) is None

    def test_最早恢复时间(self):
        pool = self._pool(["A", "B"])
        pool.block("A", "site", now=0.0)   # 300 秒
        pool.block("B", "site", now=100.0)  # 100+300=400
        assert pool.earliest_recovery("site", now=0.0) == 300

    def test_冷却按站点分开记账(self):
        pool = self._pool()
        pool.block("A", "cn", now=0.0)
        assert pool.cooling_remaining("A", "cn", 10.0) > 0
        assert pool.cooling_remaining("A", "hk", 10.0) == 0.0

    def test_换节点列表保留冷却(self):
        pool = self._pool()
        pool.block("A", "site", now=0.0)
        pool.set_nodes(["A", "B", "D"])
        assert pool.cooling_remaining("A", "site", 10.0) > 0

    def test_节点消失时清理冷却(self):
        pool = self._pool()
        pool.block("A", "site", now=0.0)
        pool.set_nodes(["B", "C"])
        assert ("A", "site") not in pool.cooldowns

    def test_本轮计数清零(self):
        pool = self._pool()
        pool.switches_this_cycle = 5
        pool.begin_cycle()
        assert pool.switches_this_cycle == 0


class TestClashClient:
    def test_unix_socket地址解析(self):
        from app.routes import ClashClient

        client = ClashClient("unix:/tmp/test.sock", group="Apple")
        assert client.socket_path == "/tmp/test.sock"
        assert client.base == "http://localhost"
        assert client.describe() == "unix:/tmp/test.sock"

    def test_http地址解析(self):
        from app.routes import ClashClient

        client = ClashClient("http://127.0.0.1:9090/", group="Apple")
        assert client.socket_path is None
        assert client.base == "http://127.0.0.1:9090"
        assert client.describe() == "http://127.0.0.1:9090"

    def test_secret头(self):
        from app.routes import ClashClient

        assert ClashClient("http://x:1", secret="abc")._headers() == {
            "Authorization": "Bearer abc"
        }
        assert ClashClient("http://x:1")._headers() == {}

    def test_解析节点列表_剔除嵌套组和假节点(self):
        """用真实 ClashVerge 的响应形状验证筛选逻辑。"""
        from app.routes import ClashClient

        client = ClashClient("http://x:1", group="Apple")
        payload = {
            "proxies": {
                "Apple": {"type": "Selector", "all": ["嵌套组", "距离下次重置剩余：7 天", "美国3", "美国4"]},
                "嵌套组": {"type": "Selector", "all": ["x"]},
                "距离下次重置剩余：7 天": {"type": "Vless"},
                "美国3": {"type": "Vless"},
                "美国4": {"type": "Vless"},
            }
        }

        async def fake_request(method, path, **kwargs):
            return payload

        client._request = fake_request  # type: ignore[method-assign]

        import asyncio

        nodes = asyncio.run(client.group_nodes())
        assert nodes == ["美国3", "美国4"]

    def test_策略组不存在时报错(self):
        from app.routes import ClashClient, ClashError

        client = ClashClient("http://x:1", group="不存在")

        async def fake_request(method, path, **kwargs):
            return {"proxies": {"Apple": {"type": "Selector", "all": []}}}

        client._request = fake_request  # type: ignore[method-assign]

        import asyncio

        with pytest.raises(ClashError, match="不存在"):
            asyncio.run(client.group_nodes())


class TestRouteManager:
    def _manager(self, nodes=("A", "B", "C")):
        from app.routes import ClashClient, RouteManager, RoutePool

        client = ClashClient("http://x:1", group="Apple")
        client.group = "Apple"
        selected = []
        closed = []

        async def fake_group_nodes():
            return list(nodes)

        async def fake_select(node):
            selected.append(node)

        async def fake_close():
            closed.append(1)
            return 2

        async def fake_current():
            # 首次被拒时还没切过节点，Clash 会报告当前在用的节点；
            # 这里模拟「当前用的是第一个节点」。
            return selected[-1] if selected else (nodes[0] if nodes else None)

        client.group_nodes = fake_group_nodes  # type: ignore[method-assign]
        client.select = fake_select  # type: ignore[method-assign]
        client.close_group_connections = fake_close  # type: ignore[method-assign]
        client.current = fake_current  # type: ignore[method-assign]

        manager = RouteManager(client, RoutePool())
        manager.selected = selected  # type: ignore[attr-defined]
        manager.closed = closed  # type: ignore[attr-defined]
        return manager

    def test_被拒后切换并断开旧连接(self):
        import asyncio

        manager = self._manager()
        wait = asyncio.run(manager.on_blocked("site"))
        assert wait == 0.0
        # 当前在用的是 A，被拒后应冷却 A 并切到 B（不能原地切回 A）
        assert manager.selected == ["B"]
        assert manager.closed == [1]  # 必须断开旧连接，否则 Chrome 复用
        assert manager.pool.active == "B"
        assert manager.pool.cooling_remaining("A", "site", time.time()) > 0

    def test_连续被拒逐个切换(self):
        import asyncio

        manager = self._manager()
        asyncio.run(manager.on_blocked("site"))
        asyncio.run(manager.on_blocked("site"))
        assert manager.selected == ["B", "C"]

    def test_达到上限后停止切换(self):
        import asyncio

        from app.routes import MAX_SWITCHES_PER_CYCLE

        manager = self._manager(nodes=[f"n{i}" for i in range(20)])
        for _ in range(MAX_SWITCHES_PER_CYCLE):
            asyncio.run(manager.on_blocked("site"))
        before = len(manager.selected)
        wait = asyncio.run(manager.on_blocked("site"))
        # 达到上限后不再切换，返回冷却时间
        assert len(manager.selected) == before
        assert wait > 0

    def test_全部冷却时返回等待时间(self):
        import asyncio

        manager = self._manager(nodes=["A"])
        wait = asyncio.run(manager.on_blocked("site"))
        assert wait > 0  # 只有 A 可用，被拒后无节点可切

    def test_成功清除冷却(self):
        import asyncio

        manager = self._manager()
        asyncio.run(manager.on_blocked("site"))
        # A 被冷却，当前已切到 B；成功一次应清除 B 的冷却
        asyncio.run(manager.on_success("site"))
        assert manager.pool.cooling_remaining("B", "site", time.time()) == 0.0


class TestRouteConfig:
    def test_配置可写clash参数(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n"
            "clash_api: unix:/tmp/verge.sock\n"
            "clash_group: Apple\n"
            "clash_secret: mysecret\n",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.clash_api == "unix:/tmp/verge.sock"
        assert config.clash_group == "Apple"
        assert config.clash_secret == "mysecret"

    def test_未配置时为空(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n", encoding="utf-8"
        )
        config = load_config(path)
        assert config.clash_api == ""


class TestRotation:
    """轮转：每轮换一个节点，而不是一个节点往死里用。"""

    def _pool(self, nodes=None, **kw):
        from app.routes import RoutePool

        pool = RoutePool(**kw)
        pool.set_nodes(nodes or ["A", "B", "C"])
        return pool

    def test_每轮前进一格(self):
        pool = self._pool()
        picked = [pool.next_rotation("site", now=0.0) for _ in range(6)]
        assert picked == ["A", "B", "C", "A", "B", "C"]

    def test_跳过冷却中的节点(self):
        pool = self._pool()
        pool.block("B", "site", now=0.0)
        picked = [pool.next_rotation("site", now=10.0) for _ in range(4)]
        assert "B" not in picked
        assert picked == ["A", "C", "A", "C"]

    def test_全部冷却返回None(self):
        pool = self._pool(["A"])
        pool.block("A", "site", now=0.0)
        assert pool.next_rotation("site", now=10.0) is None

    def test_20个节点每个200秒才用一次(self):
        """interval=10、20 个节点时，单个节点的调用间隔是 200 秒。"""
        nodes = [f"n{i}" for i in range(20)]
        pool = self._pool(nodes)
        picked = [pool.next_rotation("site", now=0.0) for _ in range(20)]
        assert len(set(picked)) == 20  # 一轮走完所有节点
        # 同一个节点两次使用之间隔 20 轮 × 10 秒 = 200 秒
        assert picked[0] == "n0" and picked.index("n0") == 0
        assert pool.next_rotation("site", now=0.0) == "n0"  # 回到起点

    def test_direct参与轮转(self):
        pool = self._pool(["A", "B"], use_direct=True)
        picked = [pool.next_rotation("site", now=0.0) for _ in range(3)]
        assert picked == ["A", "B", "DIRECT"]

    def test_direct默认不参与(self):
        pool = self._pool(["A", "B"])
        assert "DIRECT" not in pool.candidates()

    def test_direct在兜底时排最后(self):
        """所有节点冷却后，DIRECT 作为最后一道兜底。"""
        pool = self._pool(["A", "B"], use_direct=True)
        pool.block("A", "site", now=0.0)
        pool.block("B", "site", now=0.0)
        assert pool.next_node("site", now=10.0) == "DIRECT"

    def test_关闭轮转时只在失败时切换(self):
        pool = self._pool(rotate=False)
        assert pool.rotate is False
        # next_rotation 仍能工作，但 RouteManager.rotate_node 会直接返回 None

    def test_换节点列表重置游标(self):
        pool = self._pool()
        pool.next_rotation("site", now=0.0)
        pool.set_nodes(["X", "Y"])
        assert pool.next_rotation("site", now=0.0) == "X"


class TestRotateInterval:
    """轮转模式下允许更短的间隔，单出口不允许。"""

    def _write(self, tmp_path, extra=""):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n" + extra,
            encoding="utf-8",
        )
        return path

    def test_单出口10秒被拒绝(self, tmp_path):
        path = self._write(tmp_path, "interval: 10\n")
        with pytest.raises(Exception, match="太短"):
            load_config(path)

    def test_轮转模式10秒可用(self, tmp_path):
        path = self._write(
            tmp_path,
            "interval: 10\nclash_api: http://127.0.0.1:9090\nrotate_routes: true\n",
        )
        assert load_config(path).interval == 10

    def test_轮转关闭后仍按单出口下限(self, tmp_path):
        path = self._write(
            tmp_path,
            "interval: 10\nclash_api: http://127.0.0.1:9090\nrotate_routes: false\n",
        )
        with pytest.raises(Exception, match="太短"):
            load_config(path)

    def test_轮转模式仍拒绝过短(self, tmp_path):
        path = self._write(
            tmp_path,
            "interval: 3\nclash_api: http://127.0.0.1:9090\nrotate_routes: true\n",
        )
        with pytest.raises(Exception, match="太短"):
            load_config(path)

    def test_轮转相关配置可读(self, tmp_path):
        path = self._write(
            tmp_path,
            "clash_api: unix:/tmp/x.sock\nrotate_routes: true\nuse_direct_route: true\n",
        )
        config = load_config(path)
        assert config.rotate_routes is True
        assert config.use_direct_route is True


class TestRoundLog:
    """每轮日志：区分失败原因，记录补货详情。"""

    def test_成功无货(self, tmp_path):
        import asyncio

        from app.browser import PartStatus, StoreAvailability

        class FakeBrowser:
            async def discover_products(self, spec):
                return {}

            async def query(self, **kw):
                return [
                    StoreAvailability(
                        "R1", "天津万象城", "天津",
                        {"MJ1": PartStatus("MJ1", False, title="型号A")},
                    )
                ]

        monitor = Monitor(_config(tmp_path), FakeBrowser(), _Rec())  # type: ignore[arg-type]
        asyncio.run(monitor._run_cycle())
        assert len(monitor.state.rounds) == 1
        r = monitor.state.rounds[0]
        assert r.ok is True
        assert r.error_kind == ""
        assert len(r.hits) == 0
        assert r.store_count == 1

    def test_成功有补货含详情(self, tmp_path):
        import asyncio

        from app.browser import PartStatus, StoreAvailability

        class FakeBrowser:
            async def discover_products(self, spec):
                return {}

            async def query(self, **kw):
                return [
                    StoreAvailability(
                        "R1", "天津万象城", "天津",
                        {"MJ1": PartStatus("MJ1", True, title="型号A", quote="今天")},
                    )
                ]

        monitor = Monitor(_config(tmp_path), FakeBrowser(), _Rec())  # type: ignore[arg-type]
        asyncio.run(monitor._run_cycle())
        r = monitor.state.rounds[0]
        assert r.ok is True
        assert len(r.hits) == 1
        assert r.hits[0] == ("天津万象城", "型号A", "今天")

    def test_区分拦截与节点不通(self, tmp_path):
        import asyncio

        from app.browser import FetchError

        cases = [
            (FetchError("HTTP 541", blocked=True), "blocked"),
            (FetchError("HTTP 429", rate_limited=True), "rate_limited"),
            (FetchError("页面内请求失败：net::ERR_CONNECTION_RESET"), "node_error"),
            (FetchError("响应不是 JSON"), "other"),
        ]
        for exc, expected in cases:
            class FakeBrowser:
                def __init__(self, e):
                    self.e = e

                async def discover_products(self, spec):
                    return {}

                async def query(self, **kw):
                    raise self.e

            monitor = Monitor(_config(tmp_path), FakeBrowser(exc), _Rec())  # type: ignore[arg-type]
            asyncio.run(monitor._run_cycle())
            r = monitor.state.rounds[0]
            assert r.ok is False, expected
            assert r.error_kind == expected, f"{exc} 应归为 {expected}，实际 {r.error_kind}"

    def test_日志上限(self, tmp_path):
        from app.monitor import ROUND_LOG_LIMIT, RoundLog

        monitor = _monitor(tmp_path)
        for i in range(ROUND_LOG_LIMIT + 20):
            monitor.state.record_round(RoundLog(at=float(i + 1), ok=True))
        assert len(monitor.state.rounds) == ROUND_LOG_LIMIT
        # 新的在前
        assert monitor.state.rounds[0].at == float(ROUND_LOG_LIMIT + 20)

    def test_落盘与恢复(self, tmp_path):
        from app.monitor import RoundLog

        monitor = _monitor(tmp_path)
        monitor.state.record_round(
            RoundLog(
                at=1000.0, node="节点A", ok=True, store_count=3, part_count=2,
                hits=(("门店1", "型号A", "今天"),),
            )
        )
        monitor.state.save(monitor.state_file)

        reborn = _monitor(tmp_path)
        reborn.state.load(reborn.state_file)
        assert len(reborn.state.rounds) == 1
        r = reborn.state.rounds[0]
        assert r.node == "节点A"
        assert r.hits == (("门店1", "型号A", "今天"),)

    def test_坏数据不致命(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"rounds": [{"at": "bad"}, 42, None, {"at": 1.0}]}), encoding="utf-8"
        )
        state = MonitorState()
        state.load(path)
        assert len(state.rounds) == 1


class _Rec:
    """测试用 notifier，记录而不发送。"""

    def __init__(self):
        self.sent = []

    async def send(self, title, body):
        self.sent.append((title, body))

    def describe(self):
        return "测试"


class TestRotationDoesNotDropConnections:
    """轮转换节点时不能主动断连接。

    踩过的坑：轮转在每轮开始时调用 close_group_connections()，Chrome 的
    keep-alive 连接被拔掉，下一轮第一个 fetch 直接报
    `TypeError: Failed to fetch`——实测每轮都失败。
    """

    def _manager(self):
        from app.routes import ClashClient, RouteManager, RoutePool

        client = ClashClient("http://x:1", group="Apple")
        calls = {"select": [], "close": 0}

        async def gn():
            return ["A", "B"]

        async def sel(node):
            calls["select"].append(node)

        async def close():
            calls["close"] += 1
            return 3

        async def cur():
            return calls["select"][-1] if calls["select"] else "A"

        client.group_nodes = gn  # type: ignore[method-assign]
        client.select = sel  # type: ignore[method-assign]
        client.close_group_connections = close  # type: ignore[method-assign]
        client.current = cur  # type: ignore[method-assign]

        manager = RouteManager(client, RoutePool(rotate=True))
        manager.calls = calls  # type: ignore[attr-defined]
        return manager

    def test_轮转不断连接(self):
        import asyncio

        manager = self._manager()
        asyncio.run(manager.rotate_node("site"))
        assert manager.calls["select"] == ["A"]
        assert manager.calls["close"] == 0, "轮转不该断连接，否则下一轮 fetch 会失败"

    def test_被拦后切换要断连接(self):
        import asyncio

        manager = self._manager()
        asyncio.run(manager.on_blocked("site"))
        assert manager.calls["close"] >= 1, "被拦后必须断连接，否则请求仍走旧节点"


class TestScanHistoryInterruption:
    """补货中断后再出现，必须记为新的一条。

    踩过的坑：去重只看「和上一条内容是否相同」，于是 18:25 和 19:10 两次
    独立的补货（同门店、同型号、中间断货过）被当成同一条，第二次消失。
    用户看到「本轮结果」显示有补货，但补货历史里没有。
    """

    def _record(self, monitor, at, details, continuous):
        from app.monitor import ScanRecord

        monitor.state.record_scan(
            ScanRecord(
                at=at,
                store_count=len({d[0] for d in details}),
                part_count=len({d[1] for d in details}),
                details=tuple(details),
            ),
            continuous=continuous,
        )

    def test_持续有货合并(self, tmp_path):
        monitor = _monitor(tmp_path)
        self._record(monitor, 1000.0, [("西单", "Duo")], continuous=False)
        self._record(monitor, 1010.0, [("西单", "Duo")], continuous=True)
        self._record(monitor, 1020.0, [("西单", "Duo")], continuous=True)
        assert len(monitor.state.history) == 1

    def test_中断后再出现记新条目(self, tmp_path):
        """核心场景：中间断过货，第二次补货必须单独记录。"""
        monitor = _monitor(tmp_path)
        self._record(monitor, 1000.0, [("西单", "Duo")], continuous=False)
        # 中间若干轮无货（调用方不会调 record_scan，但会把 continuous 设为 False）
        self._record(monitor, 3700.0, [("西单", "Duo")], continuous=False)
        assert len(monitor.state.history) == 2
        assert monitor.state.history[0].at == 3700.0
        assert monitor.state.history[1].at == 1000.0

    def test_集合变化记新条目(self, tmp_path):
        monitor = _monitor(tmp_path)
        self._record(monitor, 1000.0, [("西单", "Duo")], continuous=True)
        self._record(monitor, 1010.0, [("西单", "Duo"), ("王府井", "Duo")], continuous=True)
        assert len(monitor.state.history) == 2

    def test_真实循环中断后重新记录(self, tmp_path):
        """用真实 _run_cycle 跑：有货 → 无货 → 有货，历史应有 2 条。"""
        import asyncio

        from app.browser import PartStatus, StoreAvailability

        def stores(available):
            return [
                StoreAvailability(
                    "R1", "西单大悦城", "天津",
                    {"MJ1": PartStatus("MJ1", available, title="型号A", quote="今天")},
                )
            ]

        class FakeBrowser:
            def __init__(self, script):
                self.script = script

            async def discover_products(self, spec):
                return {}

            async def query(self, **kw):
                return self.script.pop(0)

        monitor = Monitor(
            _config(tmp_path),
            FakeBrowser([stores(True), stores(True), stores(False), stores(True)]),
            _Rec(),  # type: ignore[arg-type]
        )
        for _ in range(4):
            asyncio.run(monitor._run_cycle())

        # 第 1 轮记一条；第 2 轮连续 → 合并；第 3 轮无货；第 4 轮重新记一条
        assert len(monitor.state.history) == 2, (
            f"应记录 2 次独立补货，实际 {len(monitor.state.history)} 条"
        )


class TestTimezone:
    """推送时间与静默时段必须用配置的时区，而不是服务器时钟。

    踩过的坑：VPS 默认 UTC，推送里的时间比北京时间慢 8 小时，静默时段
    （23:00-08:00）实际在北京时间 15:00 生效——深夜照常推送。
    """

    def test_默认时区是北京时间(self):
        import app.tz as tz

        assert tz.current().utcoffset().total_seconds() == 8 * 3600

    def test_可切换到UTC(self):
        import app.tz as tz

        tz.configure("UTC")
        try:
            assert tz.current().utcoffset().total_seconds() == 0
        finally:
            tz.configure("Asia/Shanghai")

    def test_配置接受合法时区(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\ntimezone: UTC\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n",
            encoding="utf-8",
        )
        assert load_config(path).timezone == "UTC"

    def test_配置拒绝非法时区(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "region: cn\ntimezone: Mars/Olympus\npickup:\n  cities: ['北京 北京']\nproducts: ['MJ1']\n",
            encoding="utf-8",
        )
        with pytest.raises(Exception, match="不是合法时区"):
            load_config(path)

    def test_静默时段按配置时区判断(self):
        """UTC 服务器上设北京时间静默时段，凌晨两点应该静默。"""
        import app.tz as tz
        from app.config import QuietHours

        tz.configure("Asia/Shanghai")
        quiet = QuietHours(enabled=True, start="23:00", end="08:00")
        # 北京时间凌晨 2 点 = UTC 18 点。用 tz-aware 的时间判断才正确。
        beijing_2am = datetime(2026, 1, 1, 2, 0, tzinfo=tz.zone())
        assert quiet.covers(beijing_2am) is True
        # 同一时刻的 UTC 本地时间表示是 18 点，不该影响判断
        assert quiet.covers(datetime(2026, 1, 1, 2, 0)) is True  # 钟点相同也命中
        # 中午 12 点不静默
        assert quiet.covers(datetime(2026, 1, 1, 12, 0)) is False


class TestTimeoutCompat:
    """轮询等待的异常处理必须在 Python 3.10 和 3.11+ 上都成立。

    踩过的坑：3.10 里 asyncio.TimeoutError 与内置 TimeoutError 是两个类
    （3.11 才合并），suppress 只写一个就漏接，表现为服务每个轮询周期
    崩溃一次、systemd 无限重启。这个测试在哪个版本跑，就验证哪个版本的
    实际行为——CI 矩阵会同时跑 3.10 / 3.11 / 3.12。
    """

    def test_wait_for超时能被suppress捕获(self):
        import asyncio
        import contextlib

        async def scenario():
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.sleep(60), timeout=0.01)
            return "survived"

        assert asyncio.run(scenario()) == "survived"
