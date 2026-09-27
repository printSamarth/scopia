"""PHP fixture — same four shapes, to prove the language seam is real."""

from __future__ import annotations

PROVIDERS = ["Stripe", "Paypal", "Braintree", "Klarna", "Affirm", "ApplePay"]
CONFIGS = ["billing", "orders", "inventory", "notifications", "search"]


def _provider(name: str, *, with_refund: bool, clamped: bool = True) -> str:
    body = f"""<?php

class {name}Provider implements Refundable
{{
    private $orderTotal;

    public function fees($amount)
    {{
        return clamp($amount * 0.03, 0, 100);
    }}
"""
    if with_refund:
        tail = (
            "        return clamp($refund, 0, $this->orderTotal);\n"
            if clamped
            else "        return $refund;\n"
        )
        body += f"""
    public function calculateRefund($amount)
    {{
        $refund = $amount - $this->fees($amount);
{tail}    }}
"""
    body += "}\n"
    return body


def base_files() -> dict[str, str]:
    files = {
        "src/Refundable.php": "<?php\n\ninterface Refundable\n{\n"
        "    public function calculateRefund($amount);\n}\n",
        "src/Util.php": "<?php\n\nfunction clamp($value, $low, $high)\n{\n"
        "    if ($value < $low) {\n        return $low;\n    }\n"
        "    return min($value, $high);\n}\n",
        "src/CheckoutController.php": "<?php\n\nclass CheckoutController\n{\n"
        "    public function placeOrder($cart)\n    {\n"
        "        return $cart->total;\n    }\n}\n",
        "src/DiscountService.php": "<?php\n\nclass DiscountService\n{\n"
        "    public function price($cart)\n    {\n"
        "        $engine = new PricingEngine();\n        return $engine->total($cart);\n"
        "    }\n}\n",
        "src/PricingEngine.php": "<?php\n\nclass PricingEngine\n{\n"
        "    public function total($cart)\n    {\n"
        "        return clamp($cart->total, 0, 1000000);\n    }\n}\n",
    }
    for name in PROVIDERS:
        files[f"src/Providers/{name}Provider.php"] = _provider(name, with_refund=False)
    for name in CONFIGS:
        files[f"config/{name}.php"] = f"<?php\n\nreturn ['sdk' => '4.1.2', 'name' => '{name}'];\n"
    return files


def head_files() -> dict[str, str]:
    files = base_files()

    files["src/CheckoutController.php"] = (
        "<?php\n\nclass CheckoutController\n{\n"
        "    public function placeOrder($cart)\n    {\n"
        "        return $cart->total;\n    }\n\n"
        "    public function applyPromoCode($cart, $code)\n    {\n"
        "        $service = new DiscountService();\n"
        "        return $service->validate($cart, $code);\n    }\n}\n"
    )
    files["src/DiscountService.php"] = (
        "<?php\n\nclass DiscountService\n{\n"
        "    public function price($cart)\n    {\n"
        "        $engine = new PricingEngine();\n        return $engine->total($cart);\n"
        "    }\n\n"
        "    public function validate($cart, $code)\n    {\n"
        "        $engine = new PricingEngine();\n"
        "        return $engine->recalculate($cart, $code);\n    }\n}\n"
    )
    files["src/PricingEngine.php"] = (
        "<?php\n\nclass PricingEngine\n{\n"
        "    public function total($cart)\n    {\n"
        "        return clamp($cart->total, 0, 1000000);\n    }\n\n"
        "    public function recalculate($cart, $discount)\n    {\n"
        "        $adjusted = $cart->total - $discount;\n"
        "        return clamp($adjusted, 0, $cart->total);\n    }\n}\n"
    )

    # One line inside clamp(), which every provider calls.
    files["src/Util.php"] = (
        "<?php\n\nfunction clamp($value, $low, $high)\n{\n"
        "    if ($value <= $low) {\n        return $low;\n    }\n"
        "    return min($value, $high);\n}\n"
    )

    for name in PROVIDERS:
        files[f"src/Providers/{name}Provider.php"] = _provider(
            name, with_refund=True, clamped=name != "ApplePay"
        )
    for name in CONFIGS:
        files[f"config/{name}.php"] = f"<?php\n\nreturn ['sdk' => '4.2.0', 'name' => '{name}'];\n"
    return files
