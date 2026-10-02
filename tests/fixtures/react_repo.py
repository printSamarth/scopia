"""A React fixture repo containing every shape the graph must handle.

The four shapes `py_repo.py` establishes:

  1. a call chain across four files
  2. an interface with six implementers, one of which deviates
  3. five unrelated one-line config edits, connected to nothing
  4. a ONE-LINE change inside a high-fan-in helper

plus the four a React codebase adds, none of which any other adapter sees:

  5. `<PromoField />` — composition, the edge no call expression states
  6. a component written as `const` — arrow, `memo(...)`, `useCallback(...)`, a generic
     styled-component, and a zustand store
  7. a default import under a different local name than the definition carries
  8. `.tsx` calling `.ts` calling `.js` — one language, three grammars
"""

from __future__ import annotations

PROVIDERS = ["stripe", "paypal", "braintree", "klarna", "affirm", "applepay"]
CONFIGS = ["billing", "orders", "inventory", "notifications", "search"]


def _provider(name: str, *, with_refund: bool, clamped: bool = True) -> str:
    cls = name.capitalize() + "Provider"
    body = f"""import {{ Refundable }} from '../services/Refundable';
import {{ clamp }} from '../util';

export class {cls} implements Refundable {{
  constructor(private orderTotal: number) {{}}

  fees(amount: number): number {{
    return clamp(amount * 0.03, 0, 100);
  }}
"""
    if with_refund:
        tail = (
            "    return clamp(refund, 0, this.orderTotal);\n"
            if clamped
            else "    return refund;\n"
        )
        body += f"""
  calculateRefund(amount: number): number {{
    const refund = amount - this.fees(amount);
{tail}  }}
"""
    return body + "}\n"


_SHARED = {
    # The contract six providers implement. Its method has no body, so it is a target
    # for `implements` and never a call target.
    "src/services/Refundable.ts": """export interface Refundable {
  calculateRefund(amount: number): number;
}
""",
    # A second `validate`, so resolving the first one is a real question rather than
    # the only answer available.
    "src/services/AddressService.ts": """export class AddressService {
  validate(address) {
    return Boolean(address.postcode);
  }
}
""",
    # A styled-component with a type argument. This grammar mis-parses the generic
    # tagged template, so recognising it cannot depend on the tree's shape.
    "src/ui/Button.tsx": """import styled from 'styled-components';

export const Button = styled.button<{ variant?: 'primary' | 'ghost' }>`
  padding: 8px 12px;
`;
""",
    # A hook no wrapper list names: zustand's `create` builds it. Only the `use` prefix
    # on the binding says it is callable.
    "src/store/cart.ts": """import { create } from 'zustand';
import { clamp } from '../util';

export const useCart = create<CartState>((set, get) => ({
  lines: [],
  clear: () => set({ lines: [] }),
  total: () => clamp(get().lines.length, 0, 99),
}));
""",
    # Redux Toolkit: reducers live in an object handed to a factory, and the thunk's
    # only body is an arrow passed to one. Neither is bound the way a function is.
    "src/store/todos.js": """import { createSlice, createAsyncThunk } from '@reduxjs/toolkit';
import { applyPromoCode } from '../hooks/useCheckout';

export const loadTodos = createAsyncThunk('todos/load', async (cart) => applyPromoCode(cart, ''));

const todosSlice = createSlice({
  name: 'todos',
  initialState: { items: [] },
  reducers: {
    completedCleared(state) {
      state.items = state.items.filter((t) => !t.done);
    },
  },
});

export const { completedCleared } = todosSlice.actions;
export const selectTodos = (state) => state.todos.items;
""",
    # A `.jsx` component imported under a *different* local name than it is defined
    # with — the shape that silently breaks a chain when default imports are ignored.
    "src/components/PromoField.jsx": """export default function PromoField({ code, onApply }) {
  return <input value={code} onChange={onApply} />;
}
""",
}

_CHAIN_BASE = {
    "src/components/CheckoutForm.tsx": """import Field from './PromoField';

export default function CheckoutForm({ cart }) {
  return (
    <form>
      <Field code={cart.promo} />
    </form>
  );
}
""",
    "src/hooks/useCheckout.ts": """import { DiscountService } from '../services/DiscountService';

export function placeOrder(cart) {
  return { total: cart.total };
}
""",
    "src/services/DiscountService.ts": """import { PricingEngine } from './PricingEngine';

export class DiscountService {
  price(cart) {
    const engine = new PricingEngine();
    return engine.total(cart);
  }
}
""",
    "src/services/PricingEngine.ts": """import { clamp } from '../util';

export class PricingEngine {
  total(cart) {
    return clamp(cart.total, 0, 1000000);
  }
}
""",
}

_CHAIN_HEAD = {
    # `useCallback` wraps the handler, `onSubmit={...}` wires it, `<Field />` renders a
    # component the import renamed. Three React shapes on one screen, as they occur.
    "src/components/CheckoutForm.tsx": """import { useCallback } from 'react';
import Field from './PromoField';
import { applyPromoCode } from '../hooks/useCheckout';
import { Button } from '../ui/Button';
import { useCart } from '../store/cart';
import { completedCleared, selectTodos } from '../store/todos';

export default function CheckoutForm({ cart }) {
  const handleSubmit = useCallback(() => applyPromoCode(cart, cart.promo), [cart]);
  const count = useCart((s) => s.total());
  const todos = useSelector(selectTodos);
  const onClear = () => dispatch(completedCleared());
  return (
    <form onSubmit={handleSubmit} onReset={onClear}>
      <Field code={cart.promo} onApply={handleSubmit} />
      <Button variant="primary">Pay ({count})</Button>
    </form>
  );
}
""",
    "src/hooks/useCheckout.ts": """import { DiscountService } from '../services/DiscountService';

export function placeOrder(cart) {
  return { total: cart.total };
}

export function applyPromoCode(cart, code) {
  const service: DiscountService = new DiscountService();
  return service.validate(cart, code);
}
""",
    "src/services/DiscountService.ts": """import { PricingEngine } from './PricingEngine';

export class DiscountService {
  price(cart) {
    const engine = new PricingEngine();
    return engine.total(cart);
  }

  validate(cart, code) {
    if (!code) {
      return cart.total;
    }
    return new PricingEngine().recalculate(cart, code);
  }
}
""",
    "src/services/PricingEngine.ts": """import { clamp } from '../util';

export class PricingEngine {
  total(cart) {
    return clamp(cart.total, 0, 1000000);
  }

  recalculate(cart, discount) {
    const adjusted = cart.total - discount.amount;
    return clamp(adjusted, 0, cart.total);
  }
}
""",
}


def _summary(threshold: int) -> str:
    """`memo(function …)` and an arrow component, composed through JSX."""
    return f"""import {{ memo }} from 'react';
import {{ clamp }} from '../util';

export const Total = memo(function TotalInner({{ amount }}) {{
  return <span>{{clamp(amount, 0, {threshold})}}</span>;
}});

export const Summary = () => <Total amount={{0}} />;
"""


def base_files() -> dict[str, str]:
    files = dict(_SHARED)
    files.update(_CHAIN_BASE)
    files["src/components/Summary.tsx"] = _summary(100)
    # clamp() is called from every provider and both engines — high fan-in on purpose.
    # Plain `.ts`, reached from `.tsx` and `.jsx` alike.
    files["src/util.ts"] = (
        """export function clamp(value: number, low: number, high: number): number {
  if (value < low) {
    return low;
  }
  return Math.min(value, high);
}
"""
    )
    for name in PROVIDERS:
        files[f"src/providers/{name}.ts"] = _provider(name, with_refund=False)
    for name in CONFIGS:
        files[f"src/config/{name}.js"] = (
            f"export const SDK_VERSION = '4.1.2';\nexport const SERVICE = '{name}';\n"
        )
    files["src/config/notifications.js"] = (
        "export const SDK_VERSION = '4.1.1';\nexport const SERVICE = 'notifications';\n"
    )
    return files


def head_files() -> dict[str, str]:
    files = base_files()
    files.update(_CHAIN_HEAD)
    files["src/components/Summary.tsx"] = _summary(250)

    # ONE line changes inside clamp(). Nothing else in the file moves.
    files["src/util.ts"] = (
        """export function clamp(value: number, low: number, high: number): number {
  if (value <= low) {
    return low;
  }
  return Math.min(value, high);
}
"""
    )

    for name in PROVIDERS:
        files[f"src/providers/{name}.ts"] = _provider(
            name, with_refund=True, clamped=name != "applepay"
        )
    for name in CONFIGS:
        files[f"src/config/{name}.js"] = (
            f"export const SDK_VERSION = '4.2.0';\nexport const SERVICE = '{name}';\n"
        )
    return files
