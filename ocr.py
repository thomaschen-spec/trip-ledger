import io
import re

import pytesseract
from PIL import Image
from deep_translator import GoogleTranslator

AMOUNT_KEYWORDS = ["合計", "合計金額", "お会計", "総額", "小計"]
TAX_KEYWORDS = ["消費税", "内消費税", "外税", "税込", "税抜"]

NUMBER_RE = re.compile(r"[\d,]{2,}")


def _to_int(numstr):
    try:
        return int(numstr.replace(",", ""))
    except ValueError:
        return None


def _find_amount_near_keywords(lines, keywords):
    nospace_lines = [(line, line.replace(" ", "").replace("　", "")) for line in lines]
    for keyword in keywords:
        for line, nospace in nospace_lines:
            if keyword in nospace:
                nums = NUMBER_RE.findall(nospace)
                candidates = [n for n in (_to_int(n) for n in nums) if n]
                if candidates:
                    return max(candidates)
    return None


def _guess_amount(text, lines):
    amt = _find_amount_near_keywords(lines, AMOUNT_KEYWORDS)
    if amt is not None:
        return amt
    all_nums = [n for n in (_to_int(n) for n in NUMBER_RE.findall(text)) if n]
    return max(all_nums) if all_nums else None


def _guess_tax(lines):
    return _find_amount_near_keywords(lines, TAX_KEYWORDS)


def _guess_store_name(lines):
    for line in lines:
        stripped = line.strip().replace(" ", "").replace("　", "")
        if stripped and len(stripped) <= 30 and not NUMBER_RE.fullmatch(stripped):
            return stripped
    return ""


def extract_receipt(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    raw_text = pytesseract.image_to_string(img, lang="jpn")
    lines = [l for l in raw_text.splitlines() if l.strip()]

    store_name = _guess_store_name(lines)
    amount = _guess_amount(raw_text, lines)
    tax = _guess_tax(lines)

    translated_text = ""
    translated_store = ""
    try:
        if raw_text.strip():
            translated_text = GoogleTranslator(source="ja", target="zh-TW").translate(raw_text[:4500])
        if store_name:
            translated_store = GoogleTranslator(source="ja", target="zh-TW").translate(store_name)
    except Exception:
        pass

    return {
        "raw_text": raw_text,
        "translated_text": translated_text,
        "store_name": store_name,
        "translated_store": translated_store,
        "amount": amount,
        "tax": tax,
    }
