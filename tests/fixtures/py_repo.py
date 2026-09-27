"""A Python fixture repo containing every shape the graph must handle.

  1. a call chain across four files
  2. an interface with six implementers, one of which deviates
  3. five unrelated one-line config edits, connected to nothing
  4. a ONE-LINE change inside a high-fan-in helper — the regression test for
     "a 1-line change must still appear in the call chain"
"""

from __future__ import annotations

PROVIDERS = ["stripe", "paypal", "braintree", "klarna", "affirm", "applepay"]
CONFIGS = ["billing", "orders", "inventory", "notifications", "search"]


def _provider(name: str, *, with_refund: bool, clamped: bool = True) -> str:
    cls = name.capitalize() + "Provider"
    body = f'''from app.refundable import Refundable
from app.util import clamp


class {cls}(Refundable):
    def __init__(self, order_total):
        self.order_total = order_total

    def fees(self, amount):
        return clamp(amount * 0.03, 0, 100)
'''
    if with_refund:
        tail = (
            "        return clamp(refund, 0, self.order_total)\n"
            if clamped
            else "        return refund\n"
        )
        body += f'''
    def calculate_refund(self, amount):
        refund = amount - self.fees(amount)
{tail}'''
    return body


_CHAIN_BASE = {
    "app/checkout_form.py": '''from app.checkout_controller import CheckoutController


class CheckoutForm:
    def submit(self, cart):
        controller = CheckoutController()
        return controller.place_order(cart)
''',
    "app/checkout_controller.py": '''from app.discount_service import DiscountService


class CheckoutController:
    def place_order(self, cart):
        return {"total": cart.total}
''',
    "app/discount_service.py": '''from app.pricing_engine import PricingEngine


class DiscountService:
    def price(self, cart):
        return PricingEngine().total(cart)
''',
    "app/pricing_engine.py": '''from app.util import clamp


class PricingEngine:
    def total(self, cart):
        return clamp(cart.total, 0, 1_000_000)
''',
}

_CHAIN_HEAD = {
    "app/checkout_form.py": '''from app.checkout_controller import CheckoutController


class CheckoutForm:
    def submit(self, cart):
        controller = CheckoutController()
        return controller.apply_promo_code(cart, cart.promo)
''',
    "app/checkout_controller.py": '''from app.discount_service import DiscountService


class CheckoutController:
    def place_order(self, cart):
        return {"total": cart.total}

    def apply_promo_code(self, cart, code):
        service = DiscountService()
        return service.validate(cart, code)
''',
    "app/discount_service.py": '''from app.pricing_engine import PricingEngine


class DiscountService:
    def price(self, cart):
        return PricingEngine().total(cart)

    def validate(self, cart, code):
        if not code:
            return cart.total
        return PricingEngine().recalculate(cart, code)
''',
    "app/pricing_engine.py": '''from app.util import clamp


class PricingEngine:
    def total(self, cart):
        return clamp(cart.total, 0, 1_000_000)

    def recalculate(self, cart, discount):
        adjusted = cart.total - discount.amount
        return clamp(adjusted, 0, cart.total)
''',
}

_SHARED = {
    "app/refundable.py": '''class Refundable:
    def calculate_refund(self, amount):
        raise NotImplementedError
''',
    "app/__init__.py": "",
    "app/providers/__init__.py": "",
}


def base_files() -> dict[str, str]:
    files = dict(_SHARED)
    files.update(_CHAIN_BASE)
    # clamp() is called from every provider and both engines — high fan-in on purpose.
    files["app/util.py"] = '''def clamp(value, low, high):
    """Constrain value to [low, high]."""
    if value < low:
        return low
    return min(value, high)
'''
    for name in PROVIDERS:
        files[f"app/providers/{name}.py"] = _provider(name, with_refund=False)
    for name in CONFIGS:
        files[f"config/{name}.py"] = f'SDK_VERSION = "4.1.2"\nSERVICE = "{name}"\n'
    files["config/notifications.py"] = 'SDK_VERSION = "4.1.1"\nSERVICE = "notifications"\n'
    return files


def head_files() -> dict[str, str]:
    files = base_files()
    files.update(_CHAIN_HEAD)

    # ONE line changes inside clamp(). Nothing else in the file moves.
    files["app/util.py"] = '''def clamp(value, low, high):
    """Constrain value to [low, high]."""
    if value <= low:
        return low
    return min(value, high)
'''

    for name in PROVIDERS:
        files[f"app/providers/{name}.py"] = _provider(
            name, with_refund=True, clamped=name != "applepay"
        )
    for name in CONFIGS:
        files[f"config/{name}.py"] = f'SDK_VERSION = "4.2.0"\nSERVICE = "{name}"\n'
    return files
