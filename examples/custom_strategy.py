"""Example member rules: a long-only breakout, evaluated after each closed bar."""


def decide(bars, position):
    if len(bars) < 21:
        return "hold"
    if position.side == "flat" and bars[-1].close > max(b.high for b in bars[-21:-1]):
        return "long"
    if position.side == "long" and bars[-1].close < sum(b.close for b in bars[-10:]) / 10:
        return "flat"
    return "hold"
