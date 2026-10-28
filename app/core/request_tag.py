"""请求级标签：把一次网关调用关联回调用方的切片。

用途只有对账：``logs/timing/<date>.jsonl`` 里会带上调用方声明的 tag，
方便``grep 'chunk-2'`` 查出某个切片到底调了几次、每次多久。

**不参与任何调度决策**：不改候选顺序、不影响限速、不影响冷却。

关于多 Key 的边界（不要再反复讨论）：
    初号机 / 子号1 / 子号2 分属 **三个独立的 Google 账号**，
    各自有独立的项目与独立配额（已用 Google AI Studio 的 Rate Limit 页面逐一核实）。
    因此按 ``(key, model)`` 独立建令牌桶是正确的，
    不需要做「同一项目内多 Key 共享配额」的聚合。
"""
from contextvars import ContextVar

request_tag: ContextVar[str] = ContextVar("request_tag", default="")
