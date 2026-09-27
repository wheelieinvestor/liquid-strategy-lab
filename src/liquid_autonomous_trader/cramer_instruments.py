"""Constrained issuer identity and user-approved macro instrument meanings."""

import html
import re

# These are meaning mappings, not model-selected substitute securities.
MACRO_COINS = {"CL": "xyz:CL", "SP500": "xyz:SP500"}


# Reviewed issuer aliases, not model-supplied ticker associations. Extend only
# with authoritative issuer evidence. NVIDIA confirms its NVDA ticker at:
# https://investor.nvidia.com/investor-resources/faqs/default.aspx
# Additional issuer evidence: investor.apple.com/faq/;
# investor.oracle.com/stock-information/default.aspx;
# intc.com/filings-reports/all-sec-filings/content/0000050863-26-000060/0000050863-26-000060.pdf;
# investors.palantir.com/files/2024%20FY%20PLTR%2010-K.pdf.
KNOWN_ISSUERS = {
    "NVDA": frozenset({"nvidia", "nvidia corporation"}),
    "AAPL": frozenset({"apple", "apple inc.", "apple inc"}),
    "INTC": frozenset({"intel", "intel corporation"}),
    "ORCL": frozenset({"oracle", "oracle corporation"}),
    "PLTR": frozenset({"palantir", "palantir technologies", "palantir technologies inc."}),
}


def explicit_ticker_grounded(post, call):
    """A ticker must identify the quoted claim, not another claim in the post.

    Single-letter symbols and ordinary-word collisions need a cashtag or a
    verified issuer name. In particular the S in S&P is not SentinelOne.
    """
    if call.evidence not in post.text:
        return False
    text = html.unescape(call.evidence)
    token = re.escape(call.ticker)
    boundary = r"(?![\w&/]|\.[A-Za-z0-9])"
    if re.search(r"(?<![\w$])\$" + token + boundary, text, re.I):
        return True
    collisions = {"ON", "IT", "ARE", "ALL", "CAN", "FOR", "NOW", "SO", "BE", "OR"}
    return (
        len(call.ticker) > 1
        and call.ticker not in collisions
        and bool(re.search(r"(?<![\w$&./])" + token + boundary, text))
    )


def _evidence_contexts(post, call):
    """Retain nearby subject qualifiers omitted by a model-selected excerpt."""
    interpretation = getattr(call, "interpretation", None)
    if interpretation is not None:
        start = interpretation.evidence_start
        end = interpretation.evidence_end
        if post.text[start:end] != call.evidence:
            return []
        starts = [start]
    else:
        starts = [match.start() for match in re.finditer(re.escape(call.evidence), post.text)]
    contexts = []
    for start in starts:
        prefix = post.text[max(0, start - 64) : start]
        prefix = re.split(r"[.!?;,\n]", prefix)[-1]
        contexts.append(html.unescape(prefix + call.evidence).casefold())
    return contexts


def named_issuer_grounded(post, call, data):
    """Bind a named issuer to both a verified ticker and the quoted source."""
    if call.evidence not in post.text:
        return False
    issuer = call.issuer.strip().casefold()
    known = KNOWN_ISSUERS.get(call.ticker, frozenset())
    metadata_names = {
        value.strip().casefold()
        for key in ("name", "displayName", "companyName")
        if isinstance(value := data.get(key), str) and value.strip()
    }
    if not issuer or issuer not in known | metadata_names:
        return False
    # Different official forms of the same reviewed name may occur in model
    # output and source (e.g. NVIDIA Corporation / nvidia). Otherwise require the
    # exact provider-verified name; never accept an invented issuer association.
    names = known if issuer in known else {issuer}
    evidence = html.unescape(call.evidence).casefold()
    return any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", evidence) for name in names)


def macro_grounded(post, call):
    """Require the approved subject in both the post and its exact evidence."""
    if call.ticker not in MACRO_COINS or call.evidence not in post.text:
        return False
    text = html.unescape(call.evidence).casefold()
    contexts = _evidence_contexts(post, call)
    if not contexts:
        return False
    if call.ticker == "CL":
        # Brent and oil-company equities are not WTI. Other named oil contracts
        # require their own approved mapping instead of a silent substitution.
        if any(
            re.search(r"\b(brent|oil\s+(?:stocks?|companies|equities))\b", context)
            for context in contexts
        ):
            return False
        return bool(re.search(r"\b(?:oil|crude|wti)\b|\$CL\b", text, re.I))
    if re.search(r"\bs\s*&\s*p(?:\s*500)?\b|\bsp500\b", text):
        return True
    if any(
        re.search(
            r"\b(?:oil|bond|housing|real estate|crypto|labor|labour|jobs?|rates?|"
            r"currency|forex|credit|gold|silver|energy|commodit(?:y|ies)|"
            r"foreign|global|international|emerging|chinese|china|japanese|japan|"
            r"european|europe|canadian|canada|british|german|french|indian|"
            r"asian|australian|hong kong|uk|nasdaq)(?:\s+stock)?\s+markets?\b",
            context,
        )
        for context in contexts
    ):
        return False
    return bool(re.search(r"\b(?:stock\s+market|equities|market)\b", text))
