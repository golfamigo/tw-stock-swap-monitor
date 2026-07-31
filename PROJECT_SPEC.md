# 通用資產換股／輪動監控系統
## Project Specification v1.0

> 文件用途：供 Codex、工程團隊、架構審查與 Zeabur 部署使用。  
> 核心原則：**程式碼只提供通用能力，所有可變業務規則均由資料庫、設定檔或版本化策略模板驅動。不得將特定股票、使用者持股、候選群組、價格門檻、分批比例、排程頻率、模型名稱或通知對象寫死在程式碼。**

---

## 1. 專案摘要

建立一套可部署於 Zeabur、由 GitHub 管理原始碼的多使用者資產輪動監控系統。

使用者可自行設定：

- 哪些持有資產是「換出來源」
- 哪些資產是「長期核心／受保護部位」
- 哪些資產屬於候選觀察群組
- 一個換出來源可對應哪些候選群組
- 掃描頻率與交易時段
- 技術指標與硬規則
- 接近觸發／正式觸發條件
- 候選資產評分方式與權重
- 分批換股階段與比例
- 單筆、單日、單標的及整體風險限制
- 交易成本、稅費、最小交易單位及現金緩衝
- LLM 供應商、模型、Prompt 及輸出 Schema
- LINE、Email、Telegram、Discord、Webhook 等通知管道
- 行情來源、資料延遲容忍值與備援來源
- 紙上交易或正式監控模式

系統不預設任何特定市場或股票。台股可作為第一個落地市場，但架構應允許後續擴充至其他市場與資產類型。

---

## 2. 產品目標

### 2.1 主要目標

1. 以高頻、可設定週期掃描行情。
2. 由程式完成可驗證的數學計算與硬規則篩選。
3. 僅在接近交易決策時呼叫 LLM，降低成本與雜訊。
4. 使用 Structured Output 回傳可機器驗證的決策。
5. 由程式重新驗證價格、股數、資金與風險限制。
6. 只在需要使用者注意時發送通知。
7. 保存完整行情、指標、規則、LLM、資金試算、通知與人工執行紀錄。
8. 所有策略與門檻可由設定檔或管理介面調整。

### 2.2 非目標

v1 不包含：

- 自動向券商下單
- 保存券商登入密碼
- 保證即時行情或成交
- 保證獲利
- 讓 LLM 自行補齊缺失行情
- 由 LLM 負責股數、稅費與資金運算
- 將策略參數散落寫死在程式碼
- 以單一使用者或單一股票設計整套系統

---

## 3. 核心設計原則

### 3.1 設定驅動

所有可變項均必須存在以下任一層：

1. 系統預設
2. 市場模板
3. 策略模板
4. 使用者設定
5. 投資組合設定
6. 輪動計畫設定
7. 單次執行覆寫

優先級由低到高：

```text
system defaults
  < market template
  < strategy template
  < user override
  < portfolio override
  < rotation plan override
  < run-time override
```

### 3.2 通用領域模型

程式碼中不得出現：

```python
SOURCE_SYMBOL = "2327"
CANDIDATES = ["2330", "2345", "6669", "2308"]
```

應改為：

```python
rotation_plan.source_positions
rotation_plan.candidate_groups
rotation_plan.protected_positions
```

### 3.3 計算與判斷分離

- 行情與技術指標：程式計算
- 交易成本與股數：程式計算
- 硬規則：規則引擎執行
- 矛盾訊號整合：LLM
- 最終合法性：程式再次驗證
- 是否實際成交：使用者確認

### 3.4 不信任外部輸入

必須驗證：

- 行情資料
- LLM輸出
- 使用者設定
- 通知回覆
- 人工成交確認
- Webhook
- 市場日曆
- 第三方API狀態

### 3.5 可追溯

任何一則通知都必須能追溯至：

- 使用者
- 投資組合
- 輪動計畫
- 行情快照
- 指標快照
- 規則判斷
- LLM請求與回覆
- 資金試算
- 風險驗證
- 通知紀錄
- 人工執行確認

---

## 4. 多使用者與多投資組合模型

### 4.1 User

```yaml
User:
  id: uuid
  email: string
  display_name: string
  timezone: IANA timezone
  locale: zh-TW
  status: active|suspended
  created_at: datetime
```

### 4.2 Portfolio

```yaml
Portfolio:
  id: uuid
  user_id: uuid
  name: string
  base_currency: TWD
  market_scope:
    - TWSE
    - TPEx
  strategy_profile_id: uuid
  notification_profile_id: uuid
  enabled: true
```

### 4.3 Position

```yaml
Position:
  id: uuid
  portfolio_id: uuid
  instrument_id: uuid
  quantity: decimal
  average_cost: decimal
  role: ROTATION_SOURCE|PROTECTED_CORE|NORMAL|CASH_PROXY
  status: OPEN|CLOSED
  metadata: json
```

角色定義：

- `ROTATION_SOURCE`：可被換出的資產
- `PROTECTED_CORE`：長期核心，策略不可建議賣出
- `NORMAL`：一般持有
- `CASH_PROXY`：現金或等價資產

### 4.4 Instrument

```yaml
Instrument:
  id: uuid
  market: string
  symbol: string
  name: string
  asset_type: EQUITY|ETF|FUND|CRYPTO|OTHER
  currency: string
  lot_size: decimal
  tick_size_rule_id: uuid
  trading_calendar_id: uuid
  active: true
```

`market + symbol` 必須唯一。

---

## 5. 候選群組與輪動計畫

### 5.1 CandidateGroup

```yaml
CandidateGroup:
  id: uuid
  user_id: uuid
  name: string
  description: string
  instruments:
    - instrument_id
  scoring_profile_id: uuid
  enabled: true
```

候選群組可代表：

- AI領先股
- 高股息
- 防禦型ETF
- 半導體設備
- 金融大型股
- 任意自訂觀察池

### 5.2 RotationPlan

```yaml
RotationPlan:
  id: uuid
  portfolio_id: uuid
  name: string
  source_position_ids:
    - uuid
  candidate_group_ids:
    - uuid
  protected_position_ids:
    - uuid
  strategy_profile_id: uuid
  sizing_profile_id: uuid
  llm_profile_id: uuid
  notification_profile_id: uuid
  scan_schedule_id: uuid
  status: DRAFT|PAPER|ACTIVE|PAUSED|ARCHIVED
```

一個投資組合可同時存在多個輪動計畫，每個計畫獨立保存狀態與換股階段。

---

## 6. 設定儲存與版本化

### 6.1 設定來源

正式環境以 PostgreSQL 為主。YAML 僅用於：

- 初始種子資料
- 開發與測試
- 匯出／匯入
- 版本控制的策略模板

### 6.2 禁止事項

不得：

- 在 Python 常數寫死股票代號
- 在策略函式寫死門檻
- 在通知程式寫死持股
- 在排程器寫死交易時段
- 在 LLM Prompt 寫死候選標的
- 在 sizing 程式寫死 30/30/40
- 在 provider 寫死單一資料商

### 6.3 設定版本

```yaml
ConfigVersion:
  id: uuid
  entity_type: STRATEGY_PROFILE
  entity_id: uuid
  version: 12
  payload: json
  created_by: uuid
  created_at: datetime
  change_note: string
```

每次策略執行必須記錄當時使用的設定版本。

---

## 7. 掃描排程

### 7.1 ScanSchedule

```yaml
ScanSchedule:
  id: uuid
  timezone: Asia/Taipei
  market_calendar_id: uuid
  session_windows:
    - start: "08:57"
      end: "13:30"
      scan_interval_seconds: 180
  full_evaluation_intervals:
    - bar_interval: 15m
      trigger: ON_BAR_CLOSE
  pre_close_windows:
    - start: "13:20"
      end: "13:30"
      scan_interval_seconds: 60
  enabled: true
```

所有頻率、時段與完整評估週期均可設定。

### 7.2 排程行為

- 非交易日不掃描
- 特殊休市可由市場日曆覆寫
- worker重啟後不得重複處理同一時間窗
- 每次執行需具備 idempotency key
- 掃描延遲超過設定值時標記為 degraded

---

## 8. 市場日曆

```yaml
TradingCalendar:
  id: uuid
  market: TWSE
  timezone: Asia/Taipei
  regular_sessions:
    - weekdays: [MON, TUE, WED, THU, FRI]
      open: "09:00"
      close: "13:30"
  holidays:
    - date
  special_sessions:
    - date
      open
      close
      status
```

需支援：

- 官方來源同步
- 管理者手動覆寫
- 快取
- 來源與版本紀錄

---

## 9. 行情資料抽象層

### 9.1 Provider Interface

```python
class MarketDataProvider(Protocol):
    async def get_quotes(self, instruments: list[Instrument]) -> list[Quote]: ...
    async def get_intraday_bars(
        self,
        instrument: Instrument,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]: ...
    async def get_daily_bars(
        self,
        instrument: Instrument,
        limit: int,
    ) -> list[Bar]: ...
    async def get_market_snapshot(self, market: str) -> MarketSnapshot: ...
```

### 9.2 MarketDataProfile

```yaml
MarketDataProfile:
  id: uuid
  provider: PROVIDER_KEY
  markets:
    - TWSE
  credentials_secret_refs:
    api_key: secret://market-provider/api-key
  rate_limits:
    requests_per_minute: 60
  quote_stale_after_seconds: 180
  retry_policy:
    max_attempts: 3
    backoff_seconds: [1, 3, 8]
```

### 9.3 DataQuality

```yaml
DataQuality:
  provider: string
  fetched_at: datetime
  source_timestamp: datetime
  delay_seconds: integer
  stale: boolean
  missing_fields: list
  bars_complete: boolean
  anomalies: list
  confidence: decimal
```

資料過期或欄位不足時：

- 不得產生可執行建議
- 不得呼叫LLM猜測
- 記錄失敗原因
- 視設定通知使用者

---

## 10. K線與指標引擎

### 10.1 支援週期

由設定決定，例如：

- tick
- 1m
- 3m
- 5m
- 15m
- 30m
- 60m
- 1d
- 1w

### 10.2 Indicator Registry

```python
indicator_registry.register("vwap", VWAPIndicator)
indicator_registry.register("moving_average", MovingAverageIndicator)
indicator_registry.register("relative_strength", RelativeStrengthIndicator)
indicator_registry.register("volume_ratio", VolumeRatioIndicator)
```

### 10.3 IndicatorProfile

```yaml
IndicatorProfile:
  id: uuid
  indicators:
    - key: vwap
      params:
        session_reset: true
    - key: moving_average
      params:
        periods: [5, 20, 60, 120, 240]
        timeframe: 1d
    - key: volume_ratio
      params:
        comparison: SAME_TIME
        lookback_days: 20
    - key: intraday_structure
      params:
        timeframe: 15m
        pivot_window: 3
```

新增指標時應只新增 plugin 並註冊，不修改策略核心。

---

## 11. 規則引擎

### 11.1 目的

避免將「跌破VWAP兩次」或「量比大於1.2」寫死在 Python 判斷式。

### 11.2 建議形式

使用受控規則 DSL，v1 可採：

- JSON Logic
- CEL
- 自訂受限 expression AST

不得直接執行任意 Python 字串。

### 11.3 RuleSet 範例

```yaml
RuleSet:
  id: uuid
  name: 來源資產弱化規則
  aggregation: AT_LEAST_N
  threshold: 2
  rules:
    - id: below_vwap_repeated
      expression:
        and:
          - lt: ["source.price", "source.vwap"]
          - gte: ["source.consecutive_scans_below_vwap", 2]
      weight: 1
    - id: break_session_low
      expression:
        lt: ["source.price", "source.session_low"]
      weight: 2
    - id: declining_relative_strength
      expression:
        lt: ["source.rs_slope", 0]
      weight: 1
```

### 11.4 規則類型

- Eligibility Rules
- Trigger Rules
- Exclusion Rules
- Risk Rules
- Stage Transition Rules
- Notification Rules
- LLM Invocation Rules
- Post-LLM Validation Rules

### 11.5 Rule Evaluation Output

```yaml
RuleEvaluation:
  ruleset_id: uuid
  passed: true
  score: 3
  threshold: 2
  matched_rules:
    - below_vwap_repeated
    - declining_relative_strength
  failed_rules:
    - break_session_low
  evaluated_at: datetime
```

---

## 12. 候選評分引擎

### 12.1 ScoringProfile

```yaml
ScoringProfile:
  id: uuid
  name: 通用動能輪動評分
  normalization: MIN_MAX
  min_score_for_review: 65
  min_score_for_action: 75
  close_score_gap_for_llm: 5
  factors:
    - key: relative_strength_vs_source
      weight: 20
      direction: HIGHER_BETTER
    - key: relative_strength_vs_benchmark
      weight: 15
      direction: HIGHER_BETTER
    - key: above_vwap
      weight: 10
      direction: BOOLEAN_POSITIVE
    - key: intraday_structure
      weight: 15
      mapping:
        HIGHER_HIGH_HIGHER_LOW: 1.0
        MIXED: 0.4
        LOWER_HIGH_LOWER_LOW: 0.0
    - key: volume_confirmation
      weight: 15
      direction: HIGHER_BETTER
    - key: distance_to_resistance
      weight: 10
      direction: OPTIMAL_RANGE
      optimal_range: [1.0, 5.0]
    - key: overextension
      weight: -15
      direction: PENALTY
```

### 12.2 動態調整

可依以下因素調整：

- 市場狀態
- 波動率
- 產業趨勢
- 財報事件
- 使用者風險偏好
- LLM建議

LLM不得直接改寫設定，只能提出建議，經使用者或管理者確認後形成新版本。

---

## 13. 過度追價判斷

```yaml
OverextensionProfile:
  id: uuid
  rules:
    - metric: distance_from_vwap_pct
      operator: GT
      threshold: 2.5
    - metric: distance_from_ma20_percentile
      operator: GT
      threshold: 90
    - metric: distance_to_price_limit_pct
      operator: LT
      threshold: 2.0
    - metric: intraday_rsi
      operator: GT
      threshold: 75
      requires:
        volume_price_divergence: true
```

所有門檻可依市場、波動特性、使用者與策略模板覆寫。

---

## 14. 換股階段與配置

### 14.1 SizingProfile

```yaml
SizingProfile:
  id: uuid
  allocation_mode: STAGED
  stages:
    - stage: 1
      source_allocation_pct: 30
      entry_condition_ruleset_id: uuid
    - stage: 2
      source_allocation_pct: 30
      entry_condition_ruleset_id: uuid
    - stage: 3
      source_allocation_pct: 40
      entry_condition_ruleset_id: uuid
  cash_buffer:
    mode: FIXED
    value: 10000
  max_target_allocation_pct: 100
  max_single_trade_value: null
  allow_fractional_or_odd_lot: true
```

分批比例不得寫死。

### 14.2 支援模式

- 一次性全換
- 固定多階段
- 動態風險比例
- 固定金額
- 固定股數
- 波動率調整
- 最大虧損限制
- 候選分散配置
- 單一最佳候選

---

## 15. 交易成本與市場規則

### 15.1 TransactionCostProfile

```yaml
TransactionCostProfile:
  id: uuid
  market: TWSE
  commission_rate: decimal
  commission_discount: decimal
  minimum_commission: decimal
  sell_tax_rate: decimal
  buy_tax_rate: decimal
  slippage_model:
    type: FIXED_BPS
    value: 5
```

### 15.2 MarketTradingRules

```yaml
MarketTradingRules:
  id: uuid
  market: TWSE
  odd_lot_supported: true
  minimum_quantity: 1
  standard_lot_size: 1000
  tick_size_rule_id: uuid
  price_limit_rule_id: uuid
```

不得假設所有市場一張都是1000股。

---

## 16. 資金與股數計算

### 16.1 計算責任

由 deterministic code 完成：

- 可賣股數
- 估算賣出收入
- 稅費
- 可用現金
- 候選買入股數
- 最小單位
- 現金緩衝
- 滑價
- 最大配置
- 多候選分配

### 16.2 Sizing Request

```yaml
SizingRequest:
  source_position:
    quantity: decimal
    planned_sell_quantity: decimal
    expected_price: decimal
  targets:
    - instrument_id: uuid
      expected_price: decimal
      allocation_weight: decimal
  transaction_cost_profile_id: uuid
  market_trading_rules_id: uuid
  available_cash: decimal
  cash_buffer: decimal
```

### 16.3 Sizing Result

```yaml
SizingResult:
  source_sell_quantity: decimal
  source_remaining_quantity: decimal
  estimated_gross_proceeds: decimal
  estimated_sell_costs: decimal
  estimated_net_proceeds: decimal
  target_orders:
    - instrument_id: uuid
      quantity: decimal
      estimated_value: decimal
      estimated_costs: decimal
  remaining_cash: decimal
  validation_errors: []
```

### 16.4 必要不變條件

```text
sum(target order values + target costs)
+ cash buffer
<= available cash + net source proceeds
```

並保證：

- 股數不為負
- 不超過持有股數
- 符合最小交易單位
- 不違反最大配置
- 不動到 protected position

---

## 17. LLM 決策層

### 17.1 Provider 抽象

```python
class LLMProvider(Protocol):
    async def decide(
        self,
        request: DecisionRequest,
        output_schema: type[BaseModel],
    ) -> DecisionResponse: ...
```

v1 可先實作 OpenAI provider，介面需允許未來加入其他模型供應商。

### 17.2 LLMProfile

```yaml
LLMProfile:
  id: uuid
  provider: OPENAI
  model: configurable-model-name
  reasoning_effort: configurable
  timeout_seconds: 30
  max_retries: 2
  prompt_template_id: uuid
  output_schema_version: "1.0"
  store_response: false
  enabled: true
```

模型名稱不得寫死於程式碼。

### 17.3 API金鑰

- 僅存於 Zeabur Secret
- 透過 secret reference 注入
- 不入資料庫明文
- 不寫入 Git
- 不輸出至日誌

OpenAI 官方 SDK 可由環境變數讀取 API key；Responses API 可用於模型呼叫，Structured Outputs 可用 JSON Schema 約束輸出格式。

參考：

- [OpenAI API Quickstart](https://platform.openai.com/docs/quickstart)
- [OpenAI API Reference](https://platform.openai.com/docs/api-reference)
- [OpenAI Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)

### 17.4 呼叫條件

```yaml
LLMInvocationPolicy:
  call_on:
    - trigger_level_in: [NEAR, ACTION]
    - candidate_score_gap_lt: 5
    - hard_rules_conflict: true
    - decision_state_changed: true
    - swap_stage_transition_pending: true
  cooldown_seconds: 300
  max_calls_per_plan_per_day: 20
```

### 17.5 LLM輸入

```yaml
DecisionRequest:
  schema_version: "1.0"
  event_id: uuid
  generated_at: datetime
  expires_at: datetime
  user_preferences: {}
  portfolio_summary: {}
  rotation_plan: {}
  market_context: {}
  source_assets: []
  protected_assets: []
  candidate_assets: []
  rule_evaluations: []
  candidate_scores: []
  sizing_scenarios: []
  data_quality: {}
  previous_decision: {}
```

不得傳送：

- API Secrets
- 券商密碼
- 無限制原始tick
- 不必要個資
- 未驗證資料

### 17.6 通用輸出 Schema

```yaml
DecisionResponse:
  schema_version: "1.0"
  data_validity: VALID|STALE|INSUFFICIENT
  action: HOLD_SOURCE|REDUCE_SOURCE_WAIT|ROTATE_TO_TARGET|STOP_ROTATION
  source_instrument_id: uuid|null
  target_instrument_ids:
    - uuid
  stage: integer
  confidence: integer
  recommended_source_sell_quantity: decimal
  recommended_target_quantities:
    instrument_id: decimal
  executable_price_zones:
    source_sell:
      low: decimal|null
      high: decimal|null
    targets:
      instrument_id:
        buy_low: decimal|null
        buy_high: decimal|null
        do_not_chase_above: decimal|null
        invalidation_price: decimal|null
  reasoning_summary:
    - string
  risk_conditions:
    - string
  next_observations:
    - string
```

### 17.7 LLM限制

LLM不得：

- 加入未授權候選資產
- 建議賣出 protected position
- 自行修改持股數
- 猜測缺失資料
- 突破風險限制
- 下單
- 更改策略設定
- 以自然語言取代必要 JSON 欄位

### 17.8 回傳後驗證

程式必須：

1. 驗證 Schema
2. 驗證資產白名單
3. 驗證股數
4. 重新執行 sizing
5. 驗證價格時效
6. 驗證風險規則
7. 驗證換股階段
8. 驗證通知去重
9. 記錄LLM與程式差異

若LLM建議與程式限制衝突：

- 以程式限制為準
- 將結果標為 `ADJUSTED_BY_VALIDATOR`
- 通知中清楚標示

---

## 18. Prompt Template 管理

```yaml
PromptTemplate:
  id: uuid
  name: 通用資產輪動決策
  version: 7
  system_template: text
  user_template: text
  variables_schema: json
  output_schema_version: "1.0"
  enabled: true
```

Prompt不得包含特定股票名稱，所有資訊透過變數注入。

核心指令：

```text
你是資產輪動風險決策助手。
只能在輸入列出的換出來源、受保護資產與候選資產範圍內判斷。
價格、股數、成本與風險限制以程式計算結果為準。
不能猜測缺失資料，不能更動受保護部位，不能加入未授權標的。
優先避免賣出仍具支撐的來源資產，追入已過度延伸的候選資產。
```

---

## 19. 通知系統

### 19.1 NotificationChannel Adapter

```python
class NotificationChannel(Protocol):
    async def send(self, message: NotificationMessage) -> SendResult: ...
```

支援：

- LINE Messaging API
- Telegram
- Email
- Discord
- Webhook
- App Push

### 19.2 NotificationProfile

```yaml
NotificationProfile:
  id: uuid
  channels:
    - type: LINE
      destination_secret_ref: secret://line/user-id
      enabled: true
  notify_levels:
    - ACTION
    - STOP
    - DATA_ERROR
  cooldown_seconds: 900
  locale: zh-TW
```

### 19.3 通知級別

```text
INFO
WATCH
NEAR
ACTION
STOP
DATA_ERROR
SYSTEM_ERROR
```

### 19.4 通知觸發

- 建議狀態改變
- 進入可執行價區
- 候選第一名改變
- 進入下一階段
- 訊號失效
- 資料過期
- provider故障
- LLM失敗
- worker異常

### 19.5 去重

```yaml
NotificationFingerprint:
  rotation_plan_id
  action
  source_asset
  target_assets
  stage
  price_zone_version
  invalidation_state
```

相同 fingerprint 在冷卻期內不得重送。

---

## 20. 使用者人工確認

系統不能假定通知後已成交。

```yaml
ExecutionConfirmation:
  id: uuid
  rotation_plan_id: uuid
  decision_id: uuid
  source_transactions:
    - instrument_id
      side
      quantity
      price
  target_transactions:
    - instrument_id
      side
      quantity
      price
  confirmed_by: USER|ADMIN|BROKER_IMPORT
  confirmed_at: datetime
```

確認方式：

- Web管理介面
- REST API
- LINE 指令
- 匯入券商成交紀錄
- 手動調整持股

只有確認後才更新 Position、RotationStage 與可用資金。

---

## 21. 狀態機

### 21.1 RotationState

```text
IDLE
WATCHING
NEAR_TRIGGER
ACTION_PENDING
ACTION_NOTIFIED
PARTIALLY_EXECUTED
WAITING_CONFIRMATION
STAGE_COMPLETED
ROTATION_COMPLETED
INVALIDATED
PAUSED
DATA_DEGRADED
```

### 21.2 StateTransition

```yaml
StateTransition:
  from_state: string
  to_state: string
  event_type: string
  reason: string
  occurred_at: datetime
  config_version: integer
```

---

## 22. PostgreSQL資料模型

最低需求：

```text
users
portfolios
instruments
positions
candidate_groups
candidate_group_members
rotation_plans
rotation_plan_sources
rotation_plan_candidate_groups
strategy_profiles
indicator_profiles
rule_sets
scoring_profiles
sizing_profiles
transaction_cost_profiles
market_trading_rules
scan_schedules
trading_calendars
market_data_profiles
llm_profiles
prompt_templates
notification_profiles
config_versions
market_snapshots
quotes
intraday_bars
daily_bars
indicator_snapshots
rule_evaluations
candidate_scores
strategy_runs
llm_requests
llm_decisions
sizing_results
notifications
rotation_states
state_transitions
execution_confirmations
system_health
audit_logs
```

大量行情資料可視規模採 PostgreSQL partitioning、TimescaleDB 或外部時序資料庫；v1 可先使用 PostgreSQL。

---

## 23. API設計

### 23.1 系統

```text
GET  /health
GET  /ready
GET  /metrics
GET  /status
POST /admin/run-once
```

### 23.2 使用者與投資組合

```text
POST   /users
GET    /users/{id}
POST   /portfolios
GET    /portfolios/{id}
PATCH  /portfolios/{id}
```

### 23.3 持股

```text
POST   /portfolios/{id}/positions
PATCH  /positions/{id}
POST   /positions/import
```

### 23.4 候選群組

```text
POST   /candidate-groups
PATCH  /candidate-groups/{id}
POST   /candidate-groups/{id}/instruments
DELETE /candidate-groups/{id}/instruments/{instrument_id}
```

### 23.5 輪動計畫

```text
POST   /rotation-plans
GET    /rotation-plans/{id}
PATCH  /rotation-plans/{id}
POST   /rotation-plans/{id}/activate
POST   /rotation-plans/{id}/pause
POST   /rotation-plans/{id}/run
GET    /rotation-plans/{id}/decisions
```

### 23.6 設定

```text
POST /strategy-profiles
POST /scoring-profiles
POST /sizing-profiles
POST /notification-profiles
POST /scan-schedules
POST /llm-profiles
```

### 23.7 成交確認

```text
POST /decisions/{id}/confirm-execution
POST /decisions/{id}/reject
```

---

## 24. 管理介面最低需求

至少可設定：

1. 投資組合
2. 持股與成本
3. 持股角色
4. 候選群組
5. 輪動計畫
6. 掃描頻率
7. 技術指標
8. 規則門檻
9. 評分權重
10. 分批比例
11. 交易成本
12. 通知管道
13. LLM模型與Prompt版本
14. 紙上／正式監控模式
15. 查看最新決策
16. 確認成交
17. 暫停計畫

所有高風險變更需顯示確認畫面與 audit log。

---

## 25. 技術棧

建議：

```text
Python 3.12+
FastAPI
Pydantic v2
SQLAlchemy 2
Alembic
PostgreSQL
pandas 或 polars
numpy
httpx
APScheduler 或 asyncio worker
OpenAI Python SDK
LINE Messaging API
pytest
pytest-asyncio
ruff
mypy
structlog
prometheus-client
```

前端可選：

- FastAPI server-rendered admin
- React / Next.js
- FlutterFlow
- 其他管理介面

v1 優先完成核心服務與API。

---

## 26. Zeabur部署架構

### 26.1 Services

```text
api-service
worker-service
postgres-service
optional-redis-service
```

### 26.2 API Service

- REST API
- 管理介面
- LINE webhook
- health check
- 手動run-once
- 成交確認

### 26.3 Worker Service

- 掃描排程
- 行情抓取
- 指標計算
- 規則評估
- LLM呼叫
- 通知
- 狀態轉移

### 26.4 Redis

在需要以下能力時加入：

- distributed lock
- job queue
- cooldown
- rate limiting
- event bus

單實例v1可先不用，但介面需保留。

---

## 27. Secrets

```text
DATABASE_URL
OPENAI_API_KEY
MARKET_DATA_API_KEY
LINE_CHANNEL_ACCESS_TOKEN
LINE_CHANNEL_SECRET
ENCRYPTION_KEY
ADMIN_API_TOKEN
```

規則：

- `.env.example`只列名稱
- GitHub不得出現真實值
- 使用 Zeabur Secret 管理
- 日誌遮罩
- 不回傳至前端
- 支援key rotation

---

## 28. 專案目錄

```text
generic-rotation-monitor/
├─ app/
│  ├─ main.py
│  ├─ worker.py
│  ├─ config.py
│  ├─ api/
│  ├─ domain/
│  │  ├─ users.py
│  │  ├─ portfolios.py
│  │  ├─ instruments.py
│  │  ├─ rotation_plans.py
│  │  └─ decisions.py
│  ├─ schemas/
│  ├─ repositories/
│  ├─ services/
│  ├─ data_sources/
│  │  ├─ base.py
│  │  ├─ registry.py
│  │  └─ providers/
│  ├─ indicators/
│  │  ├─ base.py
│  │  ├─ registry.py
│  │  └─ implementations/
│  ├─ rules/
│  │  ├─ engine.py
│  │  ├─ parser.py
│  │  └─ validators.py
│  ├─ scoring/
│  ├─ sizing/
│  ├─ llm/
│  │  ├─ base.py
│  │  ├─ registry.py
│  │  ├─ openai_provider.py
│  │  ├─ prompts.py
│  │  └─ validators.py
│  ├─ notifications/
│  │  ├─ base.py
│  │  ├─ registry.py
│  │  └─ channels/
│  ├─ scheduling/
│  ├─ state_machine/
│  ├─ security/
│  └─ observability/
├─ migrations/
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ contract/
│  └─ fixtures/
├─ config_templates/
│  ├─ markets/
│  ├─ strategies/
│  ├─ scoring/
│  ├─ sizing/
│  └─ notifications/
├─ docs/
│  ├─ architecture.md
│  ├─ domain-model.md
│  ├─ configuration.md
│  ├─ rule-dsl.md
│  ├─ openai-integration.md
│  ├─ line-integration.md
│  ├─ deployment-zeabur.md
│  ├─ api-contracts.md
│  └─ operations.md
├─ scripts/
├─ AGENTS.md
├─ Dockerfile.api
├─ Dockerfile.worker
├─ docker-compose.yml
├─ pyproject.toml
├─ .env.example
├─ .gitignore
└─ README.md
```

---

## 29. 程式碼規範

### 29.1 不可寫死

- 股票代號
- 使用者持股
- 成本
- 候選標的
- 市場交易時間
- 掃描間隔
- 技術門檻
- 評分權重
- 分批比例
- 手續費
- 稅率
- 通知對象
- 模型名稱
- Prompt
- Schema版本

### 29.2 可寫死

僅限：

- Enum型別
- 資料結構
- 安全不變條件
- Protocol與interface
- 通用錯誤碼
- 系統不可違反的風險限制

### 29.3 型別與驗證

- 完整型別標註
- Pydantic驗證
- Decimal處理金額
- timezone-aware datetime
- 不使用float處理精確交易金額
- 外部資料進入domain前先驗證

---

## 30. 觀測與告警

### 30.1 Metrics

```text
scan_runs_total
scan_duration_seconds
market_data_fetch_errors_total
stale_data_events_total
rule_evaluations_total
llm_calls_total
llm_failures_total
llm_latency_seconds
notifications_sent_total
notifications_failed_total
decision_adjustments_total
worker_heartbeat_timestamp
```

### 30.2 Logging

每筆log需包含：

```text
request_id
event_id
user_id
portfolio_id
rotation_plan_id
strategy_run_id
```

禁止記錄：

- API key
- access token
- 完整個資
- 未遮罩通知目的地

### 30.3 Health

- liveness
- readiness
- database
- provider
- LLM
- notification
- scheduler heartbeat

---

## 31. 失敗與降級策略

### 31.1 行情資料失敗

- 不產生ACTION
- 保存錯誤
- 根據設定通知
- 可切換備援provider
- 不使用舊行情冒充即時資料

### 31.2 LLM失敗

- 硬規則結果仍保存
- 不可冒充LLM已完成
- 可通知「LLM未完成」
- 不產生未經驗證的複雜換股建議
- 可依策略設定退回保守HOLD

### 31.3 通知失敗

- retry
- dead-letter
- 記錄錯誤
- 可切換第二通知管道

### 31.4 資料庫失敗

- 不靜默忽略
- 不更新狀態
- 避免重複通知
- 恢復後依event id去重

---

## 32. 安全與權限

### 32.1 Auth

- 使用者登入
- 管理者角色
- API token
- Webhook簽章驗證
- 最小權限

### 32.2 Multi-tenant Isolation

所有資料查詢必須具備：

```text
user_id
portfolio_id
```

不得只依資源ID查詢而忽略擁有者。

### 32.3 Audit

記錄：

- 設定變更
- 策略啟停
- 持股調整
- 人工成交確認
- Prompt更新
- LLM模型更新
- 管理者操作

---

## 33. 紙上交易模式

正式啟用前必須支援：

```text
DRAFT
PAPER
ACTIVE
```

PAPER模式：

- 照常掃描
- 照常呼叫LLM
- 照常通知
- 不改變真實持股
- 記錄假設成交
- 計算後續績效
- 支援標示「假設執行／未執行」

至少觀察5至10個交易日後才進入ACTIVE。

---

## 34. 回測與評估

需能重播歷史資料，比較：

- 硬規則決策
- LLM決策
- 是否追高
- 是否避免假突破
- 相對持有策略
- 最大回撤
- 換手成本
- 通知頻率
- 決策穩定性

每次決策保存：

```text
provider
model
model_version_or_alias
prompt_template_version
output_schema_version
strategy_config_version
```

模型或Prompt變更前需跑固定eval dataset。

---

## 35. 測試要求

### 35.1 Unit Tests

- 指標計算
- K線彙整
- 規則引擎
- 評分正規化
- 過度追價
- sizing
- 稅費
- lot size
- state transition
- fingerprint
- Schema validation

### 35.2 Integration Tests

- provider mock
- PostgreSQL
- LLM mock
- LINE mock
- worker run
- API
- 設定版本
- 多使用者隔離

### 35.3 Contract Tests

- MarketDataProvider
- LLMProvider
- NotificationChannel
- Rule DSL
- Decision Schema

### 35.4 重要案例

1. 來源資產跌破VWAP後立即站回
2. 來源資產持續轉弱
3. 候選股帶量突破
4. 候選股過度乖離
5. 候選第一、第二名接近
6. 無可買候選
7. protected asset被LLM誤建議賣出
8. LLM回傳未授權標的
9. LLM回傳過量股數
10. 行情過期
11. 部分欄位缺失
12. 交易成本造成資金不足
13. 多候選分配
14. worker重啟
15. 重複通知
16. 使用者未確認成交
17. 多投資組合同時執行
18. 市場休市
19. 特殊交易時段
20. 設定版本切換

---

## 36. 開發里程碑

### Milestone 0：領域與設定

- domain model
- database schema
- config hierarchy
- strategy template
- multi-user isolation
- ADR文件

### Milestone 1：通用核心

- mock provider
- K線與指標
- rule engine
- scoring engine
- sizing engine
- state machine
- unit tests

### Milestone 2：平台能力

- FastAPI
- PostgreSQL
- Alembic
- user/portfolio/position APIs
- candidate groups
- rotation plans
- admin run-once

### Milestone 3：真實整合

- 真實行情 provider
- 市場日曆
- LINE通知
- Zeabur部署
- observability

### Milestone 4：LLM

- OpenAI Responses API provider
- Structured Outputs
- Prompt Template
- post-LLM validation
- eval fixtures

### Milestone 5：產品化

- 管理介面
- 紙上交易
- 成交確認
- 績效與審計
- 多使用者測試

### Milestone 6：正式監控

- 5至10個交易日paper observation
- 調整設定
- ACTIVE模式
- Runbook與事故流程

---

## 37. 驗收條件

1. 新增使用者不需改程式碼。
2. 新增換出標的不需改程式碼。
3. 新增候選群組不需改程式碼。
4. 更改分批比例不需改程式碼。
5. 更改掃描頻率不需改程式碼。
6. 更改評分權重不需改程式碼。
7. 更改技術門檻不需改程式碼。
8. 更改LLM模型不需改程式碼。
9. 更改Prompt不需改程式碼。
10. 更改通知收件人不需改程式碼。
11. 新增行情provider只需新增adapter並註冊。
12. 新增通知管道只需新增adapter並註冊。
13. LLM不得操作protected position。
14. 資料過期不得產生可執行建議。
15. LLM輸出必須經程式驗證。
16. 系統不得自動下單。
17. 所有決策可追溯到資料與設定版本。
18. 多使用者資料完全隔離。
19. worker重啟後狀態可恢復。
20. 所有核心測試通過。

---

## 38. Codex第一階段任務

將本規格放在 repository 根目錄：

```text
PROJECT_SPEC.md
```

第一個 Codex 任務：

```text
請完整閱讀 PROJECT_SPEC.md，先完成 Milestone 0 與 Milestone 1 的設計及骨架。

要求：
1. 不得把任何股票代號、使用者持股、候選群組、掃描頻率、策略門檻、分批比例、交易成本、模型名稱或通知目的地寫死在程式碼。
2. 先建立多使用者、多投資組合、候選群組與輪動計畫的domain model。
3. 建立設定優先級與版本化機制。
4. 建立MarketDataProvider、Indicator、RuleEngine、ScoringEngine、SizingEngine、LLMProvider、NotificationChannel等抽象介面。
5. 使用Mock行情資料完成通用策略執行流程。
6. 建立受限規則DSL，不允許執行任意Python字串。
7. 完成state machine與idempotency設計。
8. 建立Pydantic schema、SQLAlchemy models與Alembic初始migration。
9. 加入完整型別標註、pytest、ruff與mypy。
10. 建立Dockerfile、README、AGENTS.md與架構文件。
11. 不實作自動下單。
12. 執行測試，最後列出修改檔案、測試結果、未完成事項、風險與假設。
13. 建立Pull Request，不要直接合併main。
```

---

## 39. 架構決策紀錄（ADR）

至少建立：

```text
ADR-001 configuration-driven-architecture.md
ADR-002 multi-tenant-data-isolation.md
ADR-003 rule-dsl-selection.md
ADR-004 market-data-provider-abstraction.md
ADR-005 llm-provider-and-structured-output.md
ADR-006 post-llm-deterministic-validation.md
ADR-007 no-auto-trading-in-v1.md
ADR-008 state-machine-and-idempotency.md
```

---

## 40. 最終原則

本系統的正確抽象不是：

> 監控某一檔股票，換成固定數檔股票。

而是：

> 使用者在投資組合內建立一個或多個「輪動計畫」；每個計畫定義任意換出來源、任意受保護部位、任意候選群組、任意策略模板、任意掃描排程、任意資金配置與任意通知方式。程式只負責執行被版本化的設定與安全不變條件。

任何未來需求，只要屬於「換標的、換群組、換門檻、換權重、換頻率、換模型、換通知」，都應透過設定完成，不應修改核心程式碼。
