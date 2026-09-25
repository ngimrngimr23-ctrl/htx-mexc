"""
Чистая логика качества сигналов сканера — без сети, чтобы легко тестировать:

  * find_route   — есть ли ОДНА сеть, где на бирже-источнике открыт вывод, а на
                   бирже-получателе открыт депозит; заодно сверка контрактов
                   (одинаковый тикер ≠ одна и та же монета).
  * arb_volume   — сколько $ реально можно прокрутить по стаканам обеих бирж,
                   пока спред на каждом следующем уровне не ниже порога, и какой
                   при этом средний спред.
"""
import re

# Слова, которые встречаются в названиях сетей, но сами по себе сеть не
# определяют: без этого «BNB Smart Chain» и «Base Chain» совпали бы по CHAIN.
GENERIC_WORDS = {
    "CHAIN", "SMART", "NETWORK", "MAINNET", "MAIN", "NET", "TOKEN", "THE", "NEW",
    "V2", "V3", "EVM", "C", "L1", "L2", "ONE", "PROTOCOL", "COIN",
}

# Разные написания одной сети → один идентификатор.
CANON = {
    "ERC20": "ETH", "ETH": "ETH", "ETHEREUM": "ETH",
    "BEP20": "BSC", "BSC": "BSC", "BSC20": "BSC", "BNBSMARTCHAIN": "BSC", "BEP20BSC": "BSC",
    "TRC20": "TRX", "TRX": "TRX", "TRON": "TRX",
    "SOL": "SOL", "SOLANA": "SOL", "SPL": "SOL",
    "ARBITRUM": "ARB", "ARBITRUMONE": "ARB", "ARB": "ARB", "ARBEVM": "ARB",
    "OPTIMISM": "OP", "OP": "OP", "OPETH": "OP",
    "POLYGON": "POLYGON", "MATIC": "POLYGON", "POL": "POLYGON", "PLASMA": "POLYGON",
    "AVAXC": "AVAXC", "AVAX": "AVAXC", "AVALANCHE": "AVAXC", "CCHAIN": "AVAXC",
    "BASE": "BASE",
    "SUI": "SUI",
    "APT": "APT", "APTOS": "APT",
    "TON": "TON", "TONCOIN": "TON",
    "MONAD": "MONAD", "MON": "MONAD",
    "NEAR": "NEAR",
    "KAVA": "KAVA", "KAVAEVM": "KAVA",
    "CELO": "CELO",
    "FTM": "FTM", "FANTOM": "FTM", "SONIC": "SONIC", "S": "SONIC",
    "ZKSYNC": "ZKSYNC", "ZKSYNCERA": "ZKSYNC", "ERA": "ZKSYNC",
    "LINEA": "LINEA",
    "MANTLE": "MANTLE", "MNT": "MANTLE",
    "BTC": "BTC", "BITCOIN": "BTC",
    "MAPO": "MAPO", "MAP": "MAPO", "MAPPROTOCOL": "MAPO",
}


def net_ids(*names):
    """Набор идентификаторов сети по всем её названиям на бирже."""
    ids = set()
    for name in names:
        words = re.findall(r"[A-Z0-9]+", str(name or "").upper())
        if not words:
            continue
        joined = "".join(words)
        if joined in CANON:
            ids.add(CANON[joined])
        for w in words:
            if w in CANON:
                ids.add(CANON[w])
            elif w not in GENERIC_WORDS and len(w) >= 2 and not w.isdigit():
                ids.add(w)
    return ids


_EVM_RE = re.compile(r"^0x[0-9a-f]{40}$")


def norm_contract(x):
    """Адрес контракта в сравнимом виде или None, если это не похоже на адрес.
    EVM — без учёта регистра; Sui/Aptos — «0x2::sui::SUI» == «0x000…02::sui::SUI»."""
    x = str(x or "").strip()
    if not x or x in ("0", "-", "null", "None"):
        return None
    low = x.lower()
    addr, sep, rest = low.partition("::")
    if sep and addr.startswith("0x"):
        return "0x" + (addr[2:].lstrip("0") or "0") + "::" + rest
    if _EVM_RE.match(low):
        return low
    if low.startswith("0x") and re.fullmatch(r"0x[0-9a-f]{1,64}", low):
        return "0x" + (low[2:].lstrip("0") or "0")
    # Solana/TRON и прочие base58: регистр значим, сравниваем как есть.
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{25,64}", x):
        return x
    return None


def find_route(direction, htx_chains, mexc_nets):
    """
    direction: "HTX" (купить на HTX → вывести → депозит на MEXC) или "MEXC".
    htx_chains: [{"chain", "names": [...], "withdraw", "deposit", "fee", "ca"}]
    mexc_nets:  [{"network", "names": [...], "withdraw_enable", "deposit_enable",
                  "withdraw_fee", "contract"}]

    Возвращает dict:
      state:    "open"    — есть общая сеть, открытая в нужную сторону
                "closed"  — общие сети есть, но ни одна не открыта в нужную сторону
                "nomatch" — сети бирж не удалось сопоставить по названиям
                "nodata"  — по одной из бирж нет данных о сетях
      contract: "ok" — контракт совпал хотя бы в одной общей сети
                "mismatch" — в общих по названию сетях контракты РАЗНЫЕ (разные монеты)
                "unknown" — сверить не из чего
      best:     лучшая открытая пара (самая дешёвая по комиссии вывода) или None
      pairs:    все сопоставленные пары (для текста алерта)
    """
    if not htx_chains or not mexc_nets:
        return {"state": "nodata", "contract": "unknown", "best": None, "pairs": []}

    pairs, mismatched = [], []
    any_same = False
    for h in htx_chains:
        h_ids = net_ids(*h.get("names", []))
        h_ca = norm_contract(h.get("ca"))
        for m in mexc_nets:
            m_ids = net_ids(*m.get("names", []))
            m_ca = norm_contract(m.get("contract"))
            same_ca = bool(h_ca and m_ca and h_ca == m_ca)
            diff_ca = bool(h_ca and m_ca and h_ca != m_ca)
            by_name = bool(h_ids & m_ids)
            if same_ca or (by_name and not diff_ca):
                any_same = any_same or same_ca
                if direction == "HTX":
                    is_open = bool(h.get("withdraw")) and bool(m.get("deposit_enable"))
                    fee = h.get("fee")
                else:
                    is_open = bool(m.get("withdraw_enable")) and bool(h.get("deposit"))
                    fee = m.get("withdraw_fee")
                pairs.append({"htx": h, "mexc": m, "open": is_open, "fee": fee, "same_ca": same_ca})
            elif by_name and diff_ca:
                mismatched.append((h, m))

    if any_same:
        contract = "ok"
    elif mismatched and not pairs:
        contract = "mismatch"
    else:
        contract = "unknown"

    open_pairs = [p for p in pairs if p["open"]]
    if open_pairs:
        # Сначала пары с совпавшим контрактом, затем с известной и меньшей комиссией.
        best = min(open_pairs, key=lambda p: (not p["same_ca"], p["fee"] is None, p["fee"] or 0))
        state = "open"
    else:
        best = None
        state = "closed" if pairs else ("nomatch" if not mismatched else "closed")
    return {"state": state, "contract": contract, "best": best, "pairs": pairs,
            "mismatched": mismatched}


def arb_volume(asks, bids, min_spread_pct):
    """
    asks — стакан продавцов на бирже покупки [(цена, кол-во)], по возрастанию цены.
    bids — стакан покупателей на бирже продажи [(цена, кол-во)], по убыванию цены.

    Идём по уровням с обеих сторон, пока покупка очередной порции ещё даёт спред
    не ниже min_spread_pct. Возвращает (сколько $ потратить на покупку,
    средний спред на этот объём в %, упёрлись_ли_в_конец_загруженного_стакана).
    """
    i = j = 0
    rest_a = asks[0][1] if asks else 0.0
    rest_b = bids[0][1] if bids else 0.0
    cost = proceeds = 0.0
    while i < len(asks) and j < len(bids):
        a, b = asks[i][0], bids[j][0]
        if a <= 0 or (b - a) / a * 100 < min_spread_pct:
            return cost, ((proceeds - cost) / cost * 100 if cost else None), False
        q = min(rest_a, rest_b)
        cost += q * a
        proceeds += q * b
        rest_a -= q
        rest_b -= q
        if rest_a <= 1e-12:
            i += 1
            rest_a = asks[i][1] if i < len(asks) else 0.0
        if rest_b <= 1e-12:
            j += 1
            rest_b = bids[j][1] if j < len(bids) else 0.0
    # Стакан закончился раньше, чем спред упал ниже порога: реальный объём больше.
    return cost, ((proceeds - cost) / cost * 100 if cost else None), bool(asks and bids)
