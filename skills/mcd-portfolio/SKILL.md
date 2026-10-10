---
name: mcd-portfolio
description: 体检麦当劳账户里被忽略的资产——优惠券、麦麦省待领券、积分与当月活动。把散落在麦当劳 App 各处的券汇总成一张可截图的「麦门持仓」卡片，标出每张券的到期倒计时和每日可核销时段。当用户问「我的麦当劳券什么时候过期」「我有哪些优惠券」「麦麦省能领什么券」「我的麦当劳积分」「最近有什么麦当劳活动」，或想给自己的麦当劳账户做一次体检、看看有没有券在白白浪费时使用。
agent_created: true
---

# 麦门持仓 / mcd-portfolio

## 这个 Skill 解决什么问题

麦当劳账户里的优惠券、麦麦省可领券、积分和活动信息分散在 App 的不同入口，
绝大多数用户从来不知道自己有多少张券、哪几张快过期了、哪张券今天几点才能用。

这个 Skill 把这些信息一次性拉齐，算清「哪些钱正在白白流失」，产出一张可以
直接截图发出去的持仓卡。

## 核心判断

**用户账户里最容易被浪费的不是钱，是券。**
所以卡片的主视觉永远是「待领取券」和「即将归零」，而不是积分——积分是攒出来的，
券是过期就没的。

## 前置条件

需要一个麦当劳 MCP Token（免费申请）：

1. 打开 <https://open.mcd.cn/mcp>，用手机号验证登录
2. 点右上角「控制台」→「激活」→ 同意服务协议
3. 复制得到的 MCP Token

Token 通过环境变量 `MCD_MCP_TOKEN` 传入，**不要写进任何会被提交的文件**。

## 执行方式

有两条路，优先走第一条。

### 方式一：跑脚本（推荐，结果确定）

```bash
# macOS / Linux
export MCD_MCP_TOKEN=<你的Token>
python3 scripts/mcd_portfolio.py --out card.html --json data.json
```

```powershell
# Windows PowerShell
$env:MCD_MCP_TOKEN="<你的Token>"
python scripts\mcd_portfolio.py --out card.html --json data.json
```

脚本只用 Python 标准库，会依次调用 5 个 MCP 工具、解析结果、渲染卡片。

常用参数：

| 参数 | 说明 |
|---|---|
| `--out card.html` | 卡片输出路径，默认当前目录 `card.html` |
| `--json data.json` | 额外导出结构化数据，便于二次处理 |
| `--claim` | 先调用 `auto-bind-coupons` 领光麦麦省全部券，再生成卡片（**写操作**） |
| `--template PATH` | 替换卡片模板 |

### 方式二：通过已配置的 MCP 连接器

若 WorkBuddy 已配置 `mcd-mcp` 连接器，也可直接读工具结果后手工组织，
但**倒计时、可用时段、活动排序这些计算必须交给脚本**，不要让模型心算日期。

## 工作流

1. **取数**：调用下述 5 个工具。任何一个失败都不要中断，能拿到多少算多少。
   - `now-time-info` — 服务器当前时间（所有倒计时的基准）
   - `query-my-coupons` — 已持有的券及其有效期、可用时段、适用范围
   - `available-coupons` — 麦麦省里可领取但尚未领取的券
   - `query-my-account` — 积分账户
   - `campaign-calendar` — 当月活动日历

2. **可选：领券**。若用户明确说"帮我领"或"有什么能领的都领了"，
   先调用 `auto-bind-coupons`，再重新调用 `query-my-coupons` 和 `available-coupons`
   确认领取结果。这是写操作，**用户没开口就不要替他领**。

3. **生成卡片**：运行脚本，把 `card.html` 用文件预览打开给用户看。

4. **口头总结**：不要复述卡片全部内容，只讲三件事——
   最急的那张券还有几天、麦麦省有几张没领、今天有没有值得参与的活动。

## 铁律

- **算不出来的数字不要写。** MCP 不返回券面金额，因此卡片只报「张数」和「还剩几天」，
  绝不折算成"你的账户价值 ¥XX"。编出来的数字一戳就破，比不给更糟。
- **不要替用户下单或兑换。** 本 Skill 只读，唯一允许的写操作是用户明确同意的 `auto-bind-coupons`。
- **Token 只走环境变量。** 任何情况下都不得写入 `SKILL.md`、脚本默认值或配置文件。
- **数据以官方为准。** 卡片底部必须保留"以麦当劳官方渠道为准"的声明。

## 资源

- `scripts/mcd_portfolio.py` — 取数、解析、计算、渲染，一次跑完
- `assets/card-template.html` — 卡片模板，占位符全大写且用双花括号包裹
- `references/mcp-tools.md` — 所依赖 MCP 工具的返回格式与解析要点（改了脚本再看）
