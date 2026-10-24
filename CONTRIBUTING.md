# 开发约定

## 代码风格

- Python 3.10+，用 `from __future__ import annotations`
- 行宽 100，`ruff` 检查（配置见 `pyproject.toml`）
- 中文注释与日志，代码标识符用英文

## 注释写什么

只写**代码本身表达不了的东西**：

- 为什么这么做（尤其是「看起来该那样做，但实测不行」的地方）
- 外部系统的反直觉行为（Apple 的 541、机场的假节点）
- 数值的来历（为什么是 20 个零件号、为什么退避是 5/10/20/30 分钟）

不写：

- 这行代码在做什么（读得出来）
- 「已修复 xxx」之类的变更记录（那是 commit message 的事）
- 对用户的建议（那是文档的事）

## 测试

```bash
python -m pytest tests/ -q
```

全部测试**不访问网络**。涉及外部服务的部分用假的 client / browser 替身。

新增功能时优先补这几类测试：

1. **边界条件**：空列表、单元素、超限
2. **失败路径**：外部服务返回异常、数据格式不对
3. **曾经踩过的坑**：把 bug 的触发条件写成断言，防止改回去

第 3 类最重要。`tests/test_core.py` 里几个类名直接写着「为什么」：

- `TestRotationDoesNotDropConnections` —— 轮转时断连接会导致每轮 fetch 失败
- `TestNotifyPersistence` —— 前端漏传字段导致保存静默失效
- `TestTlsContext` —— 缺根证书时 CA 数为 0

## 分层

```
app/
  config.py        配置解析与校验（不碰网络）
  configwrite.py   配置回写（保留注释）
  regions.py       地区与接口地址
  stores.py        门店目录
  browser.py       Chromium 会话与查询（唯一碰 Apple 的地方）
  routes.py        出口轮换（Clash API + 冷却池）
  monitor.py       调度、状态机、告警判定（不碰网络细节）
  notify.py        通知渠道
  dashboard.py     网页面板后端
  static/          前端（HTML/CSS/JS）
```

依赖方向单向：`cli` → `monitor` → `browser` / `routes` / `notify`。

`monitor` 不直接调用 HTTP，只通过 `browser.query()` 和 `notifier.send()`，
这样测试可以用替身替换掉它们。

## 硬性约束

这几条改坏了会出严重问题，改之前先看 `docs/DESIGN.md`：

1. **任何查询失败都不能变成「无货」**。`FetchError` 只能转成「未知」。
2. **单次请求最多 20 个零件号**。第 21 个会被 Apple 静默丢弃。
3. **大陆站的 `location=` 必须是「省 市」**，只写城市返回空门店。
4. **轮转时不主动断连接**，被拦切换时才断。
5. **前端改动要同步后端校验**，否则保存会静默失败。

## 提交

```
feat: 支持静默时段结束后补发
fix: 轮转时不断连接，避免每轮 fetch 失败
docs: 补充 ClashVerge 的 Unix socket 接入
```

正文说明**为什么**，而不是改了什么。涉及实测的写清数据。
