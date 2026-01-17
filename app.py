import os
import re
import json
import math
from typing import List, Dict, Any

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

load_dotenv()

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY missing in .env")

genai.configure(api_key=API_KEY)

PROMPT_TEXT = (
    "Analyze the food in this image. "
    "Assume a standard serving size. "
    "Identify the dish name. "
    "List the main ingredients. "
    "Estimate the calorie range (kcal). "
    "Estimate macronutrients for the serving: "
    "protein (g), carbohydrates (g), and fat (g). "
    "If the image is unclear, state uncertainty clearly. "
    "Respond briefly in clear bullet points."
)

@app.get("/ping")
def ping():
    return jsonify({"ok": True})


# --------------------- FOOD PARSING ---------------------

def _avg_from_range_text(text: str):
    """
    '30-40' -> 35
    '30 – 40' -> 35
    '30' -> 30
    """
    if text is None:
        return None
    t = text.strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", t)
    if m:
        a = float(m.group(1))
        b = float(m.group(2))
        return (a + b) / 2.0
    m2 = re.search(r"(\d+(?:\.\d+)?)", t)
    if m2:
        return float(m2.group(1))
    return None

def extract_fields(raw_text: str):
    text = (raw_text or "").strip()
    low = text.lower()

    dish_name = None
    m = re.search(r"dish name\s*[:\-]\s*(.+)", text, flags=re.IGNORECASE)
    if m:
        dish_name = m.group(1).strip()
        dish_name = re.split(r"\n|\*", dish_name)[0].strip()

    kcal_range = None
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*kcal", low)
    if m:
        kcal_range = [int(m.group(1)), int(m.group(2))]
    else:
        m = re.search(r"(\d+)\s*(?:to)\s*(\d+)\s*kcal", low)
        if m:
            kcal_range = [int(m.group(1)), int(m.group(2))]

    protein_g = None
    m = re.search(
        r"protein\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–]\s*[0-9]+(?:\.[0-9]+)?)?)\s*g",
        low
    )
    if m:
        protein_g = _avg_from_range_text(m.group(1))

    carbs_g = None
    m = re.search(
        r"(?:carbs|carbohydrates|carbohydrate)\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–]\s*[0-9]+(?:\.[0-9]+)?)?)\s*g",
        low
    )
    if m:
        carbs_g = _avg_from_range_text(m.group(1))

    fat_g = None
    m = re.search(
        r"fat\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–]\s*[0-9]+(?:\.[0-9]+)?)?)\s*g",
        low
    )
    if m:
        fat_g = _avg_from_range_text(m.group(1))

    return {
        "dishName": dish_name,
        "kcalRange": kcal_range,
        "proteinG": protein_g,
        "carbsG": carbs_g,
        "fatG": fat_g,
    }

@app.post("/api/analyze_food")
def analyze_food():
    if "image" not in request.files:
        return jsonify({"error": "Missing file field 'image'"}), 400

    file = request.files["image"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    try:
        img = Image.open(file.stream).convert("RGB")
    except Exception:
        return jsonify({"error": "Invalid image"}), 400

    model = genai.GenerativeModel("gemini-2.0-flash")

    try:
        result = model.generate_content([PROMPT_TEXT, img])
        raw_text = (result.text or "").strip()
    except Exception as e:
        return jsonify({"error": "Gemini request failed", "details": str(e)}), 500

    parsed = extract_fields(raw_text)

    return jsonify({
        "rawText": raw_text,
        "parsed": parsed
    })

# --------------------- WORKOUT PLAN (NEW) ---------------------

WORKOUT_PROMPT = """
You are a workout planner.
Goal: Burn approximately targetCalories kcal for a user with weightKg kg.

You will receive a list of candidate exercises with fields: id, name, met.
Use only exercises from the candidates list.
Return STRICT JSON ONLY in this schema (no markdown, no extra text):

{
  "planTitle": "string",
  "items": [
    {"id":"string","minutes": number, "sets": number|null, "reps": number|null, "note":"string"}
  ]
}

MANDATORY RULES:
- EXACTLY 3 to 6 DIFFERENT items
- NEVER repeat the same exercise id
- minutes must be between 3 and 20
- Prefer cardio + strength mix
- Output valid JSON even if unsure
"""


def kcal_burned(met: float, weightKg: float, minutes: float) -> float:
    return (met * weightKg * minutes) / 60.0


def fallback_plan(targetCalories: float, weightKg: float, candidates):
    """
    Gemini yoksa her zaman 3–6 farklı egzersiz üretir
    """
    cands = [c for c in candidates if float(c.get("met", 0)) > 0]
    cands.sort(key=lambda x: float(x["met"]), reverse=True)

    items = []
    remaining = targetCalories

    for c in cands:
        if len(items) >= 5:
            break

        met = float(c["met"])
        minutes = max(4.0, min(12.0, (remaining * 60) / (met * weightKg)))
        burned = kcal_burned(met, weightKg, minutes)

        items.append({
            "id": c["id"],
            "minutes": round(minutes, 1),
            "sets": None,
            "reps": None,
            "note": "Auto fallback plan"
        })

        remaining -= burned
        if remaining <= 0:
            break

    # 3 altına düşerse zorla tamamla
    if len(items) < 3:
        for c in cands:
            if c["id"] not in [i["id"] for i in items]:
                items.append({
                    "id": c["id"],
                    "minutes": 5,
                    "sets": None,
                    "reps": None,
                    "note": "Auto completed"
                })
            if len(items) >= 3:
                break

    return {
        "planTitle": "AI Workout Plan",
        "items": items[:6]
    }


def safe_json_extract(text: str):
    t = (text or "").strip()
    try:
        return json.loads(t)
    except Exception:
        start = t.find("{")
        end = t.rfind("}")
        if start != -1 and end != -1:
            return json.loads(t[start:end + 1])
    raise ValueError("Invalid JSON")


@app.post("/api/workout_plan")
def workout_plan():
    data = request.get_json(silent=True) or {}

    targetCalories = float(data.get("targetCalories", 0))
    weightKg = float(data.get("weightKg", 0))
    candidates = data.get("candidates", [])

    if targetCalories <= 0 or weightKg <= 0 or not candidates:
        return jsonify({"error": "invalid input"}), 400

    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        payload = {
            "targetCalories": targetCalories,
            "weightKg": weightKg,
            "candidates": candidates
        }

        result = model.generate_content([WORKOUT_PROMPT, json.dumps(payload)])
        plan_json = safe_json_extract(result.text or "")

        items = plan_json.get("items", [])

        # 🔒 ZORUNLU KURALLAR
        if not (3 <= len(items) <= 6):
            raise ValueError("Invalid item count")

        ids = [i.get("id") for i in items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate exercise detected")

    except Exception:
        plan_json = fallback_plan(targetCalories, weightKg, candidates)

    by_id = {c["id"]: c for c in candidates}
    out_items = []
    total_minutes = 0.0

    for it in plan_json["items"]:
        c = by_id.get(it["id"])
        if not c:
            continue

        minutes = max(3.0, min(20.0, float(it["minutes"])))
        total_minutes += minutes

        out_items.append({
            "id": c["id"],
            "name": c["name"],
            "met": c["met"],
            "minutes": minutes,
            "sets": it.get("sets"),
            "reps": it.get("reps"),
            "note": it.get("note", "")
        })

    return jsonify({
        "planTitle": plan_json.get("planTitle", "AI Workout Plan"),
        "totalMinutes": round(total_minutes, 1),
        "items": out_items
    })
