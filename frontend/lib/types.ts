export type Role = "trader" | "subscriber" | "admin";

export interface User {
  id: string;
  email: string;
  role: Role;
  display_name: string | null;
  is_active: boolean;
}

export type BrokerName = "alpaca" | "ibkr" | "webull" | "snaptrade" | "mock";

export interface BrokerAccount {
  id: string;
  broker: BrokerName;
  label: string;
  is_paper: boolean;
  supports_fractional: boolean;
  broker_account_number: string | null;
  connection_status: "pending" | "connected" | "error";
  last_error: string | null;
  created_at: string;

  cash: string | null;             // Decimal as string from API
  buying_power: string | null;
  total_equity: string | null;
  currency: string | null;
  balance_updated_at: string | null;
}


export type OrderSide = "buy" | "sell";
export type OrderType = "market" | "limit" | "stop" | "stop_limit";
export type OrderStatus =
  | "pending" | "submitted" | "accepted" | "partially_filled"
  | "filled" | "canceled" | "rejected" | "expired"
  // New: first attempt failed with a transient broker-disconnect error
  // and the subscriber opted into retry. Scheduler will try once more
  // at retry_at; if that also fails the order moves to "rejected".
  | "retry_pending";
export type InstrumentType = "stock" | "option";
export type OptionRight = "call" | "put";

// Subscriber's per-direction retry interval. NEVER = no retry, REJECT on
// first failure (legacy behaviour). Minute values = single retry attempt
// at that delay.
export type RetryInterval = "never" | "1m" | "2m" | "3m" | "5m";

export interface Fill {
  quantity: string;
  price: string;
  fee: string;
  filled_at: string;
}

export interface Order {
  id: string;
  parent_order_id: string | null;
  /** Null when the broker account was disconnected after this order was
   *  placed — history survives the disconnect. */
  broker_account_id: string | null;
  instrument_type: InstrumentType;
  symbol: string;
  side: OrderSide;
  order_type: OrderType;
  quantity: string;
  limit_price: string | null;
  stop_price: string | null;
  option_expiry: string | null;
  option_strike: string | null;
  option_right: OptionRight | null;
  status: OrderStatus;
  broker_order_id: string | null;
  filled_quantity: string;
  filled_avg_price: string | null;
  submitted_at: string | null;
  closed_at: string | null;
  reject_reason: string | null;
  created_at: string;
  /** True when this order was broadcast to subscribers via copy fanout.
   *  False for subscribers' orders, trader orders placed while copy was
   *  paused, and trader orders placed with the "Just me" scope. */
  fanned_out_to_subscribers?: boolean;
  /** True for orders created by close_trade / positions/close-all (or
   *  inherited from a closing parent). Picks subscriber's
   *  retry_interval_close instead of _open when retry is scheduled. */
  is_closing?: boolean;
  /** When the retry scheduler will attempt this order again. NULL = no
   *  retry pending. Status RETRY_PENDING + retry_at in the future = the
   *  UI should render a countdown badge. */
  retry_at?: string | null;
  /** True after the single retry attempt has been made (success or
   *  fail). The scheduler never retries an order twice. */
  retry_attempted?: boolean;
  fills: Fill[];
}

export interface Position {
  broker_account_id: string;
  broker_symbol: string;              // canonical broker id; unique key for the position
  symbol: string;
  instrument_type: InstrumentType;
  quantity: string;                  // signed: positive = long, negative = short
  avg_entry_price: string | null;
  current_price: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
  cost_basis: string | null;
  option_expiry: string | null;
  option_strike: string | null;
  option_right: OptionRight | null;
}

export interface DailyPnL {
  day: string;
  realized_pnl: string;
  trade_count: number;
}

/** A stop-loss / take-profit rule on one open position. The backend
 *  position_monitor auto-closes the position when a threshold is crossed. */
export interface PositionRule {
  id: string;
  broker_account_id: string;
  broker_symbol: string;
  take_profit_price: string | null;
  stop_loss_price: string | null;
  entry_price: string | null;
  status: "active" | "triggered" | "cancelled";
  triggered_at: string | null;
  detail: string | null;
  created_at: string;
}

export interface SubscriberSettings {
  user_id: string;
  following_trader_id: string | null;
  copy_enabled: boolean;
  multiplier: string;
  daily_loss_limit: string | null;
  /** Retry policy when a mirror order fails with a transient (broker-
   *  disconnect) error. NEVER = REJECT immediately on first failure. */
  retry_interval_open: RetryInterval;
  retry_interval_close: RetryInterval;
  todays_realized_pnl: string | null;
  /** Mirrors the followed trader's master pause. When true, the subscriber
   *  can't re-enable their own copy until the trader resumes. */
  trader_paused?: boolean;
}

/** Persistent in-app notification (bell icon + inbox page).
 *  Today the only type is "copy.retry_failed". */
export interface AppNotification {
  id: string;
  type: string;
  message: string;
  metadata: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface TraderSettings {
  user_id: string;
  trading_enabled: boolean;
  copy_paused?: boolean;
  // When True, orders the trader places DIRECTLY at their broker (outside
  // this app) are detected via the broker trade-update stream and fanned
  // out to subscribers. Default OFF.
  mirror_external_trades?: boolean;
}

export interface SubscriberSummary {
  user_id: string;
  email: string;
  display_name: string | null;
  copy_enabled: boolean;
  multiplier: string;
  broker_count: number;
  realized_pnl_30d: string;
}
