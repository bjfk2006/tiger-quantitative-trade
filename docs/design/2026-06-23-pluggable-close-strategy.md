# 设计 — 可插拔平仓策略模块（含移动止盈/回撤保护）

**Date**: 2026-06-23
**类型**: 架构增量（building-production-feature §Design）
**状态**: 待确认（未写代码）

## 1. 目标（一句话）
把现在写死在 `RiskGuard.evaluate` 的平仓决策（止盈%/止损%/收盘前强平）抽象成**可插拔策略接口 + 注册表**，开仓时按配置选择策略；新增**有状态**的「移动止盈/回撤保护」策略（如：涨超 +20% 后回落即平仓锁盈）。

**不做**：开仓策略（择时/选合约）仍由用户指定；不引入外部量化框架；不做多策略组合/投票（本期单策略）。

## 2. 现状（要改的点）
- 决策点：`strategy/risk_guard.py:28 evaluate(pnl_percent, minutes_to_close)` → 返回 `CloseReason` 或 None；优先级 时间强平 > 止损 > 止盈 > 持有。**无状态**（只看当前 pnl% + 距收盘）。
- 调用：`strategy/monitor_loop.py:run_once` 每 tick `reason = self._risk.evaluate(pnl, mtc)` → `sm.close(reason)`。
- 配置：`domain/models.py StrategyConfig`（tp_percent/sl_percent/close_buffer_minutes…）。
- `CloseReason`：TAKE_PROFIT / STOP_LOSS / TIME_FORCE_CLOSE / MANUAL。
- **痛点**：移动止盈需要跨 tick 记「峰值 pnl%」——当前无处存状态；规则写死、不可换。

## 3. 设计

### 3.1 策略接口 + 安全基类（核心）
```
class CloseStrategy(ABC):
    name: str
    def decide(self, ctx) -> Optional[CloseReason]: ...   # 每 tick 调用，返回平仓原因或 None
    def state(self) -> dict: ...            # 导出运行态(如峰值)，用于持久化
    def load_state(self, d: dict): ...      # 崩溃恢复时还原

class BaseCloseStrategy(CloseStrategy):
    """所有策略的安全底座：时间强平 + 硬止损永远生效，子类只加盈利了结逻辑。"""
    def decide(self, ctx):
        if ctx.minutes_to_close is not None and ctx.minutes_to_close <= self.close_buffer:
            return CloseReason.TIME_FORCE_CLOSE          # ① 永远最高优先级
        if ctx.pnl_percent is None:
            return None
        if ctx.pnl_percent <= -self.sl_percent:
            return CloseReason.STOP_LOSS                 # ② 硬止损兜底
        return self.profit_decide(ctx)                   # ③ 子类的盈利了结
    def profit_decide(self, ctx) -> Optional[CloseReason]: return None
```
> **关键安全决策**：时间强平 + 硬止损放在**基类**，任何策略都自带、无法被「忘记实现」绕过；策略只定制**怎么止盈**。这样换策略不会丢失下行/过夜保护。

### 3.2 具体策略
- **`ThresholdStrategy`**（默认，等价现状）：`profit_decide` = `pnl ≥ tp_percent → TAKE_PROFIT`。**完全向后兼容**。
- **`TrailingStrategy`**（新，有状态，移动止盈/回撤保护）：
  - 状态：`peak`（已 arm 后的峰值 pnl%）、`armed`（bool）。
  - 逻辑：
    ```
    if not armed and pnl >= trail_activation:  armed = True; peak = pnl
    if armed:
        peak = max(peak, pnl)
        if pnl <= peak - trail_giveback:  return CloseReason.TRAILING_STOP   # 回撤超阈值，锁盈平仓
    ```
  - 例（你的场景）：`trail_activation=20, trail_giveback=10` → 涨破 +20% 触发 arm；峰值 +20% 时回落到 +10% 即平；若继续涨到 +35%，止盈线跟到 +25%（跟随峰值上移）。
  - 仍受基类的硬止损 + 时间强平保护。

### 3.3 注册表 + 工厂
```
STRATEGY_REGISTRY = {'threshold': ThresholdStrategy, 'trailing': TrailingStrategy}
def build_strategy(name, cfg) -> CloseStrategy   # 未知名 -> 报错或回退 threshold(可配)
```

### 3.4 上下文对象
```
@dataclass
class StrategyContext:
    pnl_percent: float|None
    minutes_to_close: float|None
    market_price: float|None
    entry_price: float|None
    now_ts: int
```
（监控循环每 tick 构造，喂给 `strategy.decide`。）

### 3.5 决策优先级（每 tick，最终效果）
1. **时间强平**（距收盘 ≤ buffer）— 基类，最高
2. **硬止损**（pnl ≤ −sl%）— 基类
3. **策略盈利了结**（threshold 的 TP / trailing 的回撤）— 子类
4. 持有

### 3.6 状态持久化（崩溃恢复，关键）
- 有状态策略的 `peak/armed` 必须能跨重启恢复，否则 crash 后 trailing 重新 arm、行为错乱。
- 方案：`TradeSnapshot` 增 `strategy_name` + `strategy_state(dict)`；**监控循环每 tick（或每 N tick）把 `strategy.state()` 写进快照**（快照已是小 JSON，2s 频率可接受）。
- `resume()` 时：按 `strategy_name` 重建策略 + `load_state(snapshot.strategy_state)` 还原峰值。M1 的账户校验照旧。

## 4. 配置
| env / CLI | 说明 | 默认 |
|---|---|---|
| `OBOT_STRATEGY` / `--strategy` | `threshold` \| `trailing` | threshold |
| `OBOT_TP` / `--tp` | threshold 止盈% | 30 |
| `OBOT_SL` / `--sl` | 硬止损%（所有策略生效） | 50 |
| `OBOT_CLOSE_BUFFER` / `--close-buffer` | 收盘前强平分钟（所有策略生效） | 5/60 |
| `OBOT_TRAIL_ACTIVATION` / `--trail-activation` | trailing arm 阈值% | 20 |
| `OBOT_TRAIL_GIVEBACK` / `--trail-giveback` | trailing 回撤阈值%（从峰值） | 10 |

## 5. 影响/新增文件
| 文件 | 改动 |
|---|---|
| `strategy/close_strategies.py`（新） | `CloseStrategy`/`BaseCloseStrategy`/`ThresholdStrategy`/`TrailingStrategy`/`STRATEGY_REGISTRY`/`build_strategy` |
| `strategy/risk_guard.py` | 保留 `pre_open_check`；`evaluate` 标记 deprecated 或删（逻辑迁入 ThresholdStrategy）|
| `strategy/monitor_loop.py` | 持有 `strategy` 而非 `risk_guard.evaluate`；每 tick 构造 ctx → `strategy.decide` → 触发平仓；每 tick 持久化 strategy 状态 |
| `domain/models.py` | `StrategyConfig` 加 strategy 名 + trailing 参数；`TradeSnapshot` 加 strategy_name/strategy_state；`CloseReason` 加 `TRAILING_STOP` |
| `strategy/state_machine.py` | 开仓时按配置建策略；`_save` 写 strategy 状态；`resume` 还原 |
| `service.py` / `cli/main.py` / `config/loader.py` | 读 `OBOT_STRATEGY`/trailing 参数 / `--strategy` 等 |
| `web/dashboard.py` + 模板 | （可选）持仓行/状态展示当前策略名 + trailing 的 arm/peak |
| `tests/test_close_strategies.py`（新） | 各策略单测 |
| `.env.example` | 文档化新 env |

## 6. 核心流程（trailing 为例）
```
开仓 → build_strategy('trailing', cfg) 绑定到持仓
每 tick: ctx=(pnl, mtc...) → strategy.decide(ctx)
  - 基类先判 时间强平/硬止损
  - 未触发 → TrailingStrategy.profit_decide: 更新 peak/armed，判回撤
  - 返回 CloseReason 则 sm.close(reason)
  - 持久化 strategy.state() 进快照
崩溃重启 → resume 重建 trailing + load_state(peak,armed) 继续
```

## 7. 测试
`test_close_strategies.py`（纯逻辑，可离线跑）：
- ThresholdStrategy：与现状一致（tp/sl/time 优先级）。
- TrailingStrategy：未到 activation 不动；arm 后峰值上移；回撤达 giveback 平仓(TRAILING_STOP)；硬止损/时间强平仍生效；state()/load_state() 往返。
- build_strategy：已知名构建、未知名处理。
- 边界：pnl=None 持有；arm 后又创新高不误平。

## 8. 待确认（请你拍板）
1. **trailing 语义**：「涨超 20% 回落到 10% 平仓」我按**跟随峰值的回撤**实现（activation=20、giveback=10，峰值上移止盈线跟着上移）。
   - 备选：**固定地板**——arm 后只要 pnl 跌回到固定 +10% 就平（不跟随峰值）。
   - 你要哪种？（默认：**跟随峰值的回撤**，更符合「移动止盈」）
2. **时间强平 + 硬止损设为所有策略强制底座**（不可被策略关掉）——可否？（推荐：是，安全）
3. **每 tick 持久化策略状态**（trailing 峰值崩溃可恢复）——可否？（推荐：是）
4. 本期只做 `threshold` + `trailing` 两个策略，对吗？（以后可再加：保本止损/分批止盈/ATR 等）

确认这 4 点后，我按 §5 实现 + 单测 + 两台（香港模拟、需要时新加坡…已停）重建镜像。
