# 水利闸门调度指令核验系统（gate-guard）

调度指令**逐条**校验与上、下游水位约束是否冲突；**存在冲突的指令直接阻断，不下发执行机构**。

纯 Python 标准库实现，无任何第三方依赖（Python ≥ 3.10）。

---

## 它拦住什么

一条闸门调度指令下达后，系统会同时做两层校核：

1. **即时校核（静态规则）**——指令目标态本身是否合法
   - 目标闸门不存在 / 检修中 / 故障 → 阻断
   - 目标开度超过闸门最大开度 → 阻断
   - 目标流量为负（倒灌）或超过当前水头下泄流能力 → 阻断
   - 要求的启闭速率超过设备能力 → 阻断
   - 下发瞬间已有水池越限 → 阻断（先处置风险，禁止叠加动作）
   - 目标态上下游水位差超过结构安全上限 → 阻断；水头不足 → 预警
   - 目标态全系统下泄量低于生态基流 → 阻断

2. **前瞻校核（水量平衡模拟）**——指令执行后未来若干小时会怎样
   - 按 15 分钟步长做显式欧拉水量平衡 `dV = ΣQ·dt, dz = dV/A(z)`，
     逐时刻检查所有水池水位
   - 任一水池水位超出 `[最低水位, 最高水位]`（含汛限） → 阻断，给出**首次越限时刻**
   - 水位每小时涨/落幅度超过水池允许变率 → 阻断
   - 演进中出现倒灌 / 水位差超限 → 阻断
   - 演进中下泄量低于生态基流 → 阻断
   - 水位贴近限值（默认 0.10m 余量）但未越界 → **预警，不阻断**

判定原则：**有任意 `ERROR` 即阻断；只有 `WARNING` 仍可下发**，供调度员复核。

## 水力学模型

宽顶堰上平板闸门，公式与判别如下（系数可按率定结果在场景文件中覆盖）：

| 工况 | 判别 | 流量公式 |
|---|---|---|
| 自由闸孔出流 | `e/H₁ < 0.65`，下游未没过闸孔 | `Q = μ b e √(2g(H₁-e))` |
| 淹没闸孔出流 | 同上，但 `H₂ > e` | `Q = μ b e √(2g(H₁-H₂))` |
| 自由宽顶堰流 | `e/H₁ ≥ 0.65`，下游低于堰顶 | `Q = m b √(2g) H₁^1.5` |
| 淹没堰流 | 同上，下游高于堰顶 | 再乘 Villemonte 淹没系数 |
| 倒灌 | `H₂ > H₁` | 流量取负，作为严重冲突阻断 |

`SET_FLOW` 指令用二分法在“流量—开度”单调曲线上反推可达开度；
若目标流量超过当前水头下最大泄流能力，判定不可达并阻断。

> 该模型用于调度指令的趋势性/越限核验，不替代设计阶段的水工模型试验。

## 快速开始

```bash
cd gate-guard

# 逐条核验（有阻断时进程退出码为 2，可用于脚本/CI 串联）
python3 -m gate_guard verify examples/scenario.json examples/instructions.json

# 模拟真正的下发关口：放行的才更新系统状态，阻断的不产生任何动作
python3 -m gate_guard dispatch examples/scenario.json examples/instructions.json --json --out result.json

# 临时核验单条指令
python3 -m gate_guard check examples/scenario.json G1 SET_FLOW 30 --at +0h --duration 1 --hold 2
```

样例指令集的核验结果：1 条放行（带水位贴限预警），5 条阻断，分别命中
开度超限、启闭速率超限、全关断流+上游漫限、泄流能力不足、闸门不存在。

### 作为库使用

```python
from gate_guard import load_scenario, load_instructions, GateDispatcher
from gate_guard import BlockedInstructionError

scenario = load_scenario("scenario.json")
instructions = load_instructions("instructions.json", scenario)

dispatcher = GateDispatcher(scenario)
for ins in instructions:
    result = dispatcher.submit(ins)          # 冲突即阻断
    if result.blocked:
        for v in result.verdict.errors:
            print(v.code, v.message, "时刻:", v.at_hours)
    else:
        # result.dispatched=True：此时才允许向 PLC/SCADA 写指令
        ...

# 或要求阻断时直接抛异常
try:
    dispatcher.submit(ins, raise_on_block=True)
except BlockedInstructionError as e:
    ...
```

## 场景文件（节选）

```json
{
  "start_time": "2026-09-20T08:00:00",
  "step_hours": 0.25,
  "constraints": { "min_head_diff": 0.3, "max_head_diff": 3.5,
                   "min_total_flow": 20.0, "warning_margin": 0.1 },
  "pools": [{
    "id": "upstream", "name": "上游库区",
    "initial_level": 6.5,
    "surface_area": 2000000,
    "area_curve": [[6.0, 1800000], [6.8, 2200000]],
    "bounds": { "min": 6.0, "max": 6.8 },
    "min_level_rate": 0.5, "max_level_rate": 0.3
  }],
  "gates": [{
    "id": "G1", "upstream_pool": "upstream", "downstream_pool": "downstream",
    "width": 9.0, "sill_elevation": 4.0, "max_opening": 3.0,
    "open_rate": 0.5, "close_rate": 0.5, "initial_opening": 0.8
  }],
  "inflows":     { "upstream":   [[0, 60], [3, 120], [6, 120]] },
  "withdrawals": { "downstream": [[0, 25], [6, 25]] }
}
```

指令文件中 `issued_at` 支持绝对 ISO8601 或相对场景起点的 `+2h / +30m`；
`action` 为 `SET_OPENING`（给开度）或 `SET_FLOW`（给流量，自动反推开度）；
`hold_hours` 为目标开度保持时间，决定前瞻模拟窗口长度。

## 级联语义

批量核验/下发按指令时间排序：

- **被阻断的指令不改变任何状态**；
- 被放行指令的目标开度成为后续指令核验的起点（含设备启闭过程约束）；
- 两条指令的间隔期，系统按当时开度 + 边界来水过程自由演进水位。

用 `--no-cascade` 可让每条指令都从场景初始状态独立核验。

## 目录结构

```
gate-guard/
├── gate_guard/
│   ├── models.py       # 数据模型与约束区间
│   ├── hydraulics.py   # 孔流/堰流/淹没/倒灌，目标流量反推开度
│   ├── series.py       # 边界过程线插值
│   ├── loader.py       # JSON 场景/指令装载与配置校验
│   ├── rules.py        # 静态规则 + 轨迹规则（可扩展注册）
│   ├── engine.py       # 水量平衡前推模拟 + 逐条核验引擎
│   ├── dispatcher.py   # 阻断式下发关口
│   ├── report.py       # 中文文本 / JSON 报告
│   └── cli.py          # verify / dispatch / check
├── examples/           # 场景与指令样例
└── tests/              # 47 个单元测试（python3 -m unittest discover -t . -s tests）
```

## 扩展新约束

规则在 `rules.py` 中实现 `StaticRule.check()` / `SimulationRule.check()`
并加入 `STATIC_RULES` / `SIMULATION_RULES` 即可；规则之间相互独立，
返回的每条 `Violation` 带规则码、严重级别、发生时刻与现场量测，便于追溯。
