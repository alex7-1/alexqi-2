import base64
import json
import os
import re
from typing import Any

import anthropic
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


app = FastAPI(title="Alex Qi · 报价截图识别 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

PROMPT = """你是 Alex Qi 报价卡的数据录入助手。请从德国电力/燃气合同系统截图中提取字段，只返回 JSON，不要 Markdown，不要解释。

非常重要的数字规则：
1. monthly 必须是合同原始月费/正常月费/原价，不是“含优惠平均”的大号展示价。
2. 如果截图同时出现“大号月费”和“原价 XX /月”，monthly 取“原价 XX”。
3. yearly 是未扣新客优惠前的年度总额，通常等于 monthly × 合同期月数。
4. firstyear 是首年实付，通常等于 yearly - bonus。
5. bonus 是新客优惠金额；没有新客优惠时 bonus 返回 "0"，has_bonus 返回 false。
6. arbeitspreis 是 ct/kWh 单价，只要数字；grundpreis 是 €/年基础费，只要数字。
7. duration 和 guarantee 只返回月份数字，例如 "12"。
8. 德国数字格式请转成英文小数点：55,11 -> 55.11，1.234,56 -> 1234.56。
9. 不确定的字段返回空字符串，禁止编造。

返回格式必须严格如下：
{
  "supplier": "",
  "contract": "",
  "monthly": "",
  "avg_monthly": "",
  "yearly": "",
  "arbeitspreis": "",
  "grundpreis": "",
  "bonus": "",
  "has_bonus": true,
  "duration": "",
  "guarantee": "",
  "tags": "",
  "firstyear": ""
}
"""

NUMBER_FIELDS = {"monthly", "avg_monthly", "yearly", "arbeitspreis", "grundpreis", "bonus", "firstyear"}


@app.get("/")
def health():
    return {"status": "ok", "service": "Alex Qi 报价截图识别"}


def clean_json_text(raw_text: str) -> str:
    text = raw_text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text


def normalize_number(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?i)(eur|ct/kwh|ct|kwh)", "", text)
    text = text.replace("€", "").replace("欧元", "").replace("欧", "")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9,.\-]", "", text)
    if not text:
        return ""
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        num = float(text)
    except ValueError:
        return ""
    return str(int(num)) if num.is_integer() else f"{num:.2f}"


def normalize_months(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d+", text)
    return match.group(0) if match else ""


def num(value: Any) -> float:
    cleaned = normalize_number(value)
    return float(cleaned) if cleaned else 0.0


def money(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def normalize_result(result: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    data = dict(result)

    for field in NUMBER_FIELDS:
        if field in data:
            before = str(data.get(field, "")).strip()
            data[field] = normalize_number(data.get(field))
            if before and data[field] and before != data[field]:
                warnings.append(f"{field}: {before} -> {data[field]}")

    for field in ("duration", "guarantee"):
        if field in data:
            data[field] = normalize_months(data.get(field))

    if data.get("duration") and not data.get("guarantee"):
        data["guarantee"] = data["duration"]

    has_bonus = not (data.get("has_bonus") is False or data.get("bonus") in ("", "0", 0, None))
    data["has_bonus"] = bool(has_bonus)
    if not has_bonus:
        data["bonus"] = "0"

    duration = float(normalize_months(data.get("duration")) or 12)
    bonus = num(data.get("bonus"))
    monthly = num(data.get("monthly"))
    avg_monthly = num(data.get("avg_monthly"))
    yearly = num(data.get("yearly"))
    firstyear = num(data.get("firstyear"))

    # If the model put the discounted average into monthly, recover gross monthly
    # from firstyear + bonus or from yearly.
    if has_bonus and firstyear and bonus and monthly:
        discounted_avg = firstyear / duration
        gross_monthly = (firstyear + bonus) / duration
        if abs(monthly - discounted_avg) < 0.35 and abs(gross_monthly - monthly) > 0.5:
            data["monthly"] = money(gross_monthly)
            monthly = gross_monthly
            warnings.append(f"monthly looked like discounted average; corrected to {data['monthly']}")

    if avg_monthly and monthly and has_bonus:
        expected_avg = max(0, monthly - bonus / duration)
        if abs(avg_monthly - expected_avg) > 0.5:
            data["avg_monthly"] = money(expected_avg)
            warnings.append(f"avg_monthly corrected to {data['avg_monthly']}")

    if monthly:
        expected_yearly = monthly * duration
        if not yearly or abs(yearly - expected_yearly) > 1:
            data["yearly"] = money(expected_yearly)
            yearly = expected_yearly
            warnings.append(f"yearly corrected to {data['yearly']}")

    if yearly:
        expected_firstyear = max(0, yearly - (bonus if has_bonus else 0))
        if not firstyear or abs(firstyear - expected_firstyear) > 1:
            data["firstyear"] = money(expected_firstyear)
            warnings.append(f"firstyear corrected to {data['firstyear']}")

    if not data.get("tags"):
        tags = []
        if has_bonus and data.get("bonus"):
            tags.append(f"{data['bonus']}€ 新客优惠")
        if data.get("guarantee"):
            tags.append(f"{data['guarantee']}个月保价")
        data["tags"] = " / ".join(tags)

    return data, warnings


@app.post("/api/parse-screenshot")
async def parse_screenshot(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="请上传图片文件")

    img_bytes = await file.read()
    if len(img_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片太大，请压缩后重试")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="后端缺少 ANTHROPIC_API_KEY")

    img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": file.content_type,
                                "data": img_b64,
                            },
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API 错误: {exc}") from exc

    raw_text = message.content[0].text.strip()
    try:
        result = json.loads(clean_json_text(raw_text))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"识别结果解析失败: {raw_text[:300]}") from exc

    data, warnings = normalize_result(result)
    return JSONResponse(content={"success": True, "data": data, "debug": {"warnings": warnings, "raw_model_text": raw_text}})
